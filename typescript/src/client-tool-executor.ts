import type { BackendConnectionPool, BackendLink } from "./backend-pool.js";
import {
  BackendConnectionClosedError,
  BackendRequestError,
  TiDBLinkError,
} from "./errors.js";
import type {
  ClientToolCallContext,
  ClientToolHandlers,
} from "./session-connection.js";

/** A `tool.call` notification. */
interface ToolCall {
  callId: string;
  sessionId: string;
  userId: string;
  turnId: string;
  name: string;
  arguments: Record<string, unknown>;
  claimBy: string;
  deadlineAt: string;
  /** Present on a Connector call only. */
  connector?: { id: string; name: string };
}

type ClaimAnswer =
  | { claimed: true }
  | {
      claimed: false;
      reason: "call_not_found" | "superseded" | "claim_window_closed" | "call_settled";
    };
type ResultAnswer =
  | { accepted: true }
  | { accepted: false; reason: "call_not_found" | "not_claimed" | "call_settled" };

type ToolResult = { output: string } | { error: { message: string } };

/** A call refused, then submitted, then known: what the executor remembers of each callId. */
type CallState = "claiming" | "running" | "settled";

const OUTPUT_MAX = 65_536;
const ERROR_MAX = 4_096;
/** How long a settled callId is remembered to ignore late duplicates. */
const SETTLED_MEMORY_MS = 60_000;

/**
 * Raised through `onError` when the core did not accept a Client Tool
 * result. `reason` is the core's: `call_settled` (the call already had an
 * outcome, for example its deadline passed), `not_claimed` or
 * `call_not_found`.
 */
export class ClientToolResultRefusedError extends TiDBLinkError {
  constructor(
    readonly callId: string,
    readonly reason: string,
  ) {
    super(`The core did not accept the result of Client Tool call ${callId}: ${reason}`);
  }
}

const isString = (value: unknown): value is string => typeof value === "string" && value !== "";
const isObject = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

const isConnector = (value: unknown): value is { id: string; name: string } =>
  isObject(value) && isString(value.id) && isString(value.name);

const parseCall = (params: Record<string, unknown>): ToolCall | undefined =>
  (params.connector === undefined || isConnector(params.connector)) &&
  isString(params.callId) &&
  isString(params.sessionId) &&
  isString(params.userId) &&
  isString(params.turnId) &&
  isString(params.name) &&
  isObject(params.arguments) &&
  isString(params.claimBy) &&
  isString(params.deadlineAt) &&
  Number.isFinite(Date.parse(params.deadlineAt))
    ? (params as unknown as ToolCall)
    : undefined;

const truncate = (text: string, max: number): string =>
  text.length <= max ? text : `${text.slice(0, max - 1)}…`;

/** The text the model receives for a handler's return value. */
const outputOf = (value: unknown): ToolResult => {
  let output: string | undefined;
  try {
    output = typeof value === "string" ? value : value === undefined ? "" : JSON.stringify(value);
  } catch (cause) {
    return { error: { message: `The tool returned a value that is not JSON: ${String(cause)}` } };
  }
  if (output === undefined)
    return { error: { message: "The tool returned a value that is not JSON" } };
  if (output.length > OUTPUT_MAX)
    return {
      error: { message: `The tool output exceeds ${OUTPUT_MAX} characters` },
    };
  return { output };
};

const errorOf = (error: unknown): ToolResult => {
  const message = error instanceof Error ? error.message : String(error);
  return { error: { message: truncate(message || "The tool failed", ERROR_MAX) } };
};

/** Whose calls an executor runs: one Session's Client Tools, or one served Connector's functions. */
export type ToolCallSource = { sessionId: string } | { connectorName: string };

/**
 * Executes the calls an executing Session connection or a served Connector
 * receives: claim on the connection that delivered the call, run only after
 * `claimed: true`, then submit the result over any Backend Connection until
 * the call's deadline. Each callId runs at most once.
 */
export class ClientToolExecutor {
  readonly #pool: BackendConnectionPool;
  readonly #source: ToolCallSource;
  readonly #handlers: ClientToolHandlers;
  readonly #report: (error: unknown) => void;
  readonly #calls = new Map<string, CallState>();

  constructor(input: {
    pool: BackendConnectionPool;
    source: ToolCallSource;
    handlers: ClientToolHandlers;
    report: (error: unknown) => void;
  }) {
    this.#pool = input.pool;
    this.#source = input.source;
    this.#handlers = input.handlers;
    this.#report = input.report;
  }

  /** Handle one `tool.call` from the connection that delivered it. */
  receive(params: Record<string, unknown>, link: BackendLink): void {
    const call = parseCall(params);
    const source = this.#source;
    if (
      !call ||
      ("sessionId" in source
        ? call.connector !== undefined || call.sessionId !== source.sessionId
        : call.connector?.name !== source.connectorName)
    ) {
      this.#report(
        new TiDBLinkError(
          "sessionId" in source
            ? "Received an invalid Client Tool call"
            : "Received an invalid Connector call",
        ),
      );
      return;
    }
    // The core may send a call again after a reconnect; it runs once.
    if (this.#calls.has(call.callId)) return;
    // Never sent: the core sends it again to the next attachment while unclaimed.
    if (!link.open) return;
    this.#calls.set(call.callId, "claiming");
    void this.#claimAndRun(call, link);
  }

  async #claimAndRun(call: ToolCall, link: BackendLink): Promise<void> {
    let answer: ClaimAnswer;
    try {
      answer = await link.request<ClaimAnswer>("tool.claim", { callId: call.callId });
    } catch (error) {
      if (
        error instanceof BackendConnectionClosedError ||
        (error instanceof BackendRequestError && error.code === -32603)
      ) {
        // The claim may have been granted; the call must not run, so close it as failed.
        await this.#submit(call, {
          error: {
            message:
              "The Application Server lost the claim's answer and did not execute the call",
          },
        });
        return;
      }
      this.#calls.set(call.callId, "settled");
      this.#forget(call);
      this.#report(error);
      return;
    }
    if (!answer || answer.claimed !== true) {
      if (answer?.reason === "superseded") {
        // Another generation holds the Session; this process may receive it again there.
        this.#calls.delete(call.callId);
      } else {
        this.#calls.set(call.callId, "settled");
        this.#forget(call);
      }
      return;
    }
    this.#calls.set(call.callId, "running");
    await this.#submit(call, await this.#execute(call));
  }

  async #execute(call: ToolCall): Promise<ToolResult> {
    const handler = Object.hasOwn(this.#handlers, call.name)
      ? this.#handlers[call.name]
      : undefined;
    if (!handler)
      return {
        error: {
          message: call.connector
            ? `Connector ${call.connector.name} has no function ${call.name} in this Application Server`
            : `Client Tool ${call.name} has no handler in this Application Server`,
        },
      };
    const deadlineAt = new Date(call.deadlineAt);
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const expired = new Promise<ToolResult>((resolve) => {
      timer = setTimeout(
        () => {
          controller.abort(new TiDBLinkError("The Client Tool call reached its deadline"));
          resolve({ error: { message: "The tool did not finish before its deadline" } });
        },
        Math.max(0, deadlineAt.getTime() - Date.now()),
      );
    });
    const context: ClientToolCallContext = {
      callId: call.callId,
      sessionId: call.sessionId,
      userId: call.userId,
      turnId: call.turnId,
      name: call.name,
      deadlineAt,
      signal: controller.signal,
      ...(call.connector ? { connector: { id: call.connector.id, name: call.connector.name } } : {}),
    };
    const run = (async () => {
      try {
        return outputOf(await handler.run(call.arguments, context));
      } catch (error) {
        return errorOf(error);
      }
    })();
    try {
      return await Promise.race([run, expired]);
    } finally {
      clearTimeout(timer);
    }
  }

  async #submit(call: ToolCall, result: ToolResult): Promise<void> {
    try {
      const answer = await this.#pool.request<ResultAnswer>(
        "tool.result",
        { callId: call.callId, ...result },
        { retryUntil: Date.parse(call.deadlineAt) },
      );
      if (answer?.accepted !== true) {
        if (answer?.reason === "not_claimed") {
          // The lost claim never took effect: the call may arrive again and run then.
          this.#calls.delete(call.callId);
          return;
        }
        this.#report(new ClientToolResultRefusedError(call.callId, answer?.reason ?? "unknown"));
      }
    } catch (error) {
      this.#report(error);
    }
    this.#calls.set(call.callId, "settled");
    this.#forget(call);
  }

  #forget(call: ToolCall): void {
    const timer = setTimeout(
      () => this.#calls.delete(call.callId),
      Math.max(0, Date.parse(call.deadlineAt) - Date.now()) + SETTLED_MEMORY_MS,
    );
    (timer as { unref?: () => void }).unref?.();
  }
}
