import {
  BackendTransport,
  backendConnectionUrl,
  type WebSocketConstructor,
} from "./backend-connection.js";
import {
  BackendConnectionClosedError,
  BackendRequestError,
  TiDBLinkError,
} from "./errors.js";
import { sleep } from "./http.js";

/** The answer of a `session.attach` that took effect. */
export interface AttachResult {
  /** The Session's attachment generation. */
  generation: number;
  /** Position of the Session's latest event when the attachment took effect. */
  cursor: string;
}

/** Why the pool stopped serving an attachment. It does not retry it. */
export type AttachmentEnd =
  /** An executing attachment on another connection took the Session. */
  | { reason: "superseded" }
  /** The Session was deleted. */
  | { reason: "session_deleted" }
  /** `OrganizationClient.close()` closed every Backend Connection. */
  | { reason: "closed" }
  /** Re-attaching failed for good, for example after a revoked Key. */
  | { reason: "error"; error: unknown };

/** Requests over one Backend Connection. */
export interface BackendLink {
  readonly open: boolean;
  /** `onAnswer` sees a successful answer before any frame that followed it. */
  request<T = unknown>(
    method: string,
    params: Record<string, unknown>,
    onAnswer?: (result: unknown) => void,
  ): Promise<T>;
}

/**
 * The pool's view of one Session Attachment. The pool calls it; a Session
 * connection implements it.
 */
export interface PoolAttachment {
  readonly sessionId: string;
  /** Parameters of the next `session.attach`, recomputed for every (re)attach. */
  attachParams(): Record<string, unknown>;
  /** A (re)attach took effect on `link`, which now delivers the Session. */
  attached(result: AttachResult, link: BackendLink): void;
  /** A notification for this Session from the connection that holds it; `size` is its frame's UTF-8 size. */
  notify(method: string, params: Record<string, unknown>, link: BackendLink, size: number): void;
  /** The pool gave the attachment up; called at most once, never after `detach()`. */
  ended(end: AttachmentEnd): void;
}

/** Why the pool stopped serving a Connector. It does not retry it. */
export type ServingEnd =
  /** `OrganizationClient.close()` closed every Backend Connection. */
  | { reason: "closed" }
  /** No connection could serve it any more, for example after a revoked Key. */
  | { reason: "error"; error: unknown };

/**
 * The pool's view of one Connector this process serves. The pool declares it
 * on every Backend Connection; a served Connector implements it.
 */
export interface PoolConnector {
  readonly name: string;
  /** Parameters of every `connector.serve`. */
  readonly serveParams: Record<string, unknown>;
  /** A `tool.call` for this Connector from the connection that delivered it. */
  call(params: Record<string, unknown>, link: BackendLink): void;
  /** A declaration failed on one connection without ending the Connector. */
  report(error: unknown): void;
  /** The pool gave the Connector up; called at most once, never after `withdraw()`. */
  ended(end: ServingEnd): void;
}

export interface BackendConnectionPoolOptions {
  baseUrl: string;
  apiKey: string;
  WebSocket?: WebSocketConstructor | undefined;
  /** Close a connection that has held nothing for this long. Default 5 seconds. */
  idleCloseMs?: number;
  /** See `OpenBackendTransportInput.openTimeoutMs`. */
  openTimeoutMs?: number;
}

interface Slot {
  readonly transport: BackendTransport;
  /** Attachments whose notifications this connection routes, by Session. */
  readonly held: Map<string, PoolAttachment>;
  /** Attaches in flight on this connection. */
  reserved: number;
  /** The core answered `connection_full`. */
  full: boolean;
  /** The core sent `connection.drain`; nothing new goes here. */
  draining: boolean;
  idleTimer: ReturnType<typeof setTimeout> | undefined;
  /** Connectors declared, or being declared, on this connection, by name. */
  readonly serving: Map<string, Promise<string>>;
}

const DEFAULT_IDLE_CLOSE_MS = 5_000;
/** Attempts of a first attach before its caller sees the failure. */
const FIRST_ATTACH_ATTEMPTS = 3;
/** Attaches in flight across the pool; the rest wait their turn. */
const ATTACH_CONCURRENCY = 32;
const MAX_BACKOFF_MS = 30_000;
/** Spread of re-attaches after a lost connection, so that peers do not arrive at once. */
const RECOVERY_JITTER_MS = 1_000;
/** How long `close()` waits for detach answers before closing the sockets. */
const CLOSE_GRACE_MS = 2_000;

const backoff = (failures: number): number => {
  const base = Math.min(MAX_BACKOFF_MS, 250 * 2 ** Math.min(failures, 10));
  return Math.round(base / 2 + (Math.random() * base) / 2);
};

/** Failures a later attempt, possibly on another connection, can fix. */
const isTransient = (error: unknown): boolean =>
  (error instanceof BackendConnectionClosedError && error.retryable) ||
  (error instanceof BackendRequestError && error.code === -32603);

const isServeResult = (value: unknown): value is { connectorId: string } =>
  typeof value === "object" &&
  value !== null &&
  typeof (value as { connectorId?: unknown }).connectorId === "string" &&
  (value as { connectorId: string }).connectorId !== "";

const isAttachResult = (value: unknown): value is AttachResult =>
  typeof value === "object" &&
  value !== null &&
  Number.isSafeInteger((value as AttachResult).generation) &&
  (value as AttachResult).generation >= 0 &&
  typeof (value as AttachResult).cursor === "string" &&
  (value as AttachResult).cursor !== "";

/**
 * The Backend Connections of one OrganizationClient. Connections open on
 * demand and hold up to the `maxSessions` the core announces; further
 * attachments open another connection, which the load balancer may place on
 * another replica. A lost connection's attachments re-attach elsewhere with
 * their latest parameters; `connection.drain` moves them before the core
 * closes the connection. Served Connectors are declared on every connection,
 * so that a connection executing a Session also serves its Connector calls,
 * and are declared again on each new one. Connections that hold nothing close
 * after a short idle period, except the last one while a Connector is served.
 */
export class BackendConnectionPool {
  readonly #url: string;
  readonly #apiKey: string;
  readonly #WebSocket: WebSocketConstructor | undefined;
  readonly #idleCloseMs: number;
  readonly #openTimeoutMs: number | undefined;
  readonly #slots = new Set<Slot>();
  /** Attachments the application keeps, attached or being re-attached. */
  readonly #wanted = new Map<string, PoolAttachment>();
  /** The connection currently routing each attachment. */
  readonly #where = new Map<PoolAttachment, Slot>();
  /** Connectors this process serves, by name. */
  readonly #connectors = new Map<string, PoolConnector>();
  /** Connectors whose `serve()` has not settled: it reports their failures. */
  readonly #pendingServe = new Set<PoolConnector>();
  #reopening = false;
  #opening: Promise<Slot> | undefined;
  #openFailures = 0;
  #attaching = 0;
  readonly #attachQueue: (() => void)[] = [];
  #closed = false;

  constructor(options: BackendConnectionPoolOptions) {
    this.#url = backendConnectionUrl(options.baseUrl);
    this.#apiKey = options.apiKey;
    this.#WebSocket = options.WebSocket;
    this.#idleCloseMs = options.idleCloseMs ?? DEFAULT_IDLE_CLOSE_MS;
    this.#openTimeoutMs = options.openTimeoutMs;
  }

  /** Open Backend Connections, for diagnostics and tests. */
  get connections(): number {
    return this.#slots.size;
  }

  /**
   * Attach a Session and keep it attached until `detach()` or until the pool
   * gives it up through `ended()`. Rejects when the first attach fails; one
   * attachment per Session is allowed at a time.
   */
  async attach(attachment: PoolAttachment): Promise<AttachResult> {
    if (this.#closed) throw new TiDBLinkError("This OrganizationClient is closed");
    if (this.#wanted.has(attachment.sessionId))
      throw new TiDBLinkError(
        `Session ${attachment.sessionId} is already connected through this OrganizationClient; reuse that connection or close it first`,
      );
    this.#wanted.set(attachment.sessionId, attachment);
    try {
      return await this.#attach(attachment, FIRST_ATTACH_ATTEMPTS);
    } catch (error) {
      if (this.#wanted.get(attachment.sessionId) === attachment)
        this.#wanted.delete(attachment.sessionId);
      throw error;
    }
  }

  /** Release an attachment. Best effort: a lost connection releases it anyway. */
  async detach(attachment: PoolAttachment): Promise<void> {
    if (this.#wanted.get(attachment.sessionId) !== attachment) return;
    this.#wanted.delete(attachment.sessionId);
    const slot = this.#where.get(attachment);
    this.#where.delete(attachment);
    if (!slot || slot.held.get(attachment.sessionId) !== attachment) return;
    this.#release(slot, attachment.sessionId);
    try {
      await slot.transport.request("session.detach", { sessionId: attachment.sessionId });
    } catch {
      // The attachment lapses with its connection.
    } finally {
      this.#maybeIdle(slot);
    }
  }

  /**
   * Attach again on the connection that holds the attachment, with its
   * current parameters, so that the core replays the Session's events after
   * the attachment's cursor. Only for observing attachments: an executing
   * attach could take the Session back from a newer executor. Nothing happens
   * while the attachment is moving to another connection, whose attach
   * replays the same way. A transient failure is recovered like a lost
   * connection; any other ends the attachment.
   */
  async resume(attachment: PoolAttachment): Promise<void> {
    if (this.#wanted.get(attachment.sessionId) !== attachment) return;
    const slot = this.#where.get(attachment);
    if (!slot || slot.held.get(attachment.sessionId) !== attachment) return;
    const holds = () =>
      this.#wanted.get(attachment.sessionId) === attachment &&
      slot.held.get(attachment.sessionId) === attachment;
    try {
      const result = await this.#limited(async () => {
        // Closed or moved while waiting for its turn: nothing to resume.
        if (!holds()) return undefined;
        return slot.transport.request(
          "session.attach",
          attachment.attachParams(),
          // The replay may arrive with the answer: take it from the answer on.
          (answer) => {
            if (isAttachResult(answer) && holds()) attachment.attached(answer, slot.transport);
          },
        );
      });
      if (result === undefined) return;
      if (!isAttachResult(result))
        throw new TiDBLinkError("session.attach returned an invalid result");
    } catch (error) {
      if (!holds()) return;
      this.#release(slot, attachment.sessionId);
      this.#where.delete(attachment);
      slot.transport
        .request("session.detach", { sessionId: attachment.sessionId })
        .catch(() => undefined);
      this.#maybeIdle(slot);
      const reason = error instanceof BackendRequestError ? error.reason : undefined;
      if (
        !(error instanceof BackendRequestError) ||
        error.code === -32603 ||
        reason === "connection_full" ||
        reason === "connection_draining"
      ) {
        // Another connection, or a later attempt, may take it.
        void this.#recover(attachment, RECOVERY_JITTER_MS);
        return;
      }
      this.#wanted.delete(attachment.sessionId);
      safely(() =>
        attachment.ended(
          reason === "session_not_found" ? { reason: "session_deleted" } : { reason: "error", error },
        ),
      );
    }
  }

  /**
   * Send a request that any connection of the Organization may carry, such as
   * `tool.result`, retrying on another connection after transient failures
   * until `retryUntil` (epoch milliseconds). Only idempotent requests belong here.
   */
  async request<T = unknown>(
    method: string,
    params: Record<string, unknown>,
    options: { retryUntil?: number } = {},
  ): Promise<T> {
    const until = options.retryUntil ?? 0;
    for (let failures = 0; ; failures++) {
      let slot: Slot;
      try {
        slot = await this.#anySlot();
      } catch (error) {
        if (!isTransient(error) || Date.now() >= until) throw error;
        await sleep(Math.min(backoff(failures), Math.max(0, until - Date.now())));
        continue;
      }
      this.#cancelIdle(slot);
      try {
        return await slot.transport.request<T>(method, params);
      } catch (error) {
        if (!isTransient(error) || Date.now() >= until) throw error;
        if (error instanceof BackendRequestError)
          await sleep(Math.min(backoff(failures), Math.max(0, until - Date.now())));
      } finally {
        this.#maybeIdle(slot);
      }
    }
  }

  /**
   * Serve a Connector on every connection, now and on each one opened later,
   * until `withdraw()` or until the pool gives it up through `ended()`.
   * Resolves with its connectorId once one connection serves it; rejects when
   * the first declaration fails. One Connector per name is allowed at a time.
   */
  async serve(connector: PoolConnector): Promise<string> {
    if (this.#closed) throw new TiDBLinkError("This OrganizationClient is closed");
    if (this.#connectors.has(connector.name))
      throw new TiDBLinkError(
        `Connector ${connector.name} is already served through this OrganizationClient; close it first`,
      );
    this.#connectors.set(connector.name, connector);
    this.#pendingServe.add(connector);
    try {
      for (let failures = 0; ; ) {
        if (this.#connectors.get(connector.name) !== connector)
          throw new TiDBLinkError("The Connector was closed while it was being served");
        let slot: Slot | undefined;
        try {
          slot = await this.#servingSlot();
          this.#cancelIdle(slot);
          const connectorId = await this.#declare(slot, connector);
          for (const other of this.#slots)
            if (other !== slot && this.#serves(other)) void this.#declareQuietly(other, connector);
          return connectorId;
        } catch (error) {
          failures += 1;
          if (error instanceof BackendRequestError && error.reason === "connection_draining") {
            if (slot) slot.draining = true;
          } else if (!isTransient(error)) throw error;
          if (failures >= FIRST_ATTACH_ATTEMPTS) throw error;
          await sleep(backoff(failures));
        } finally {
          if (slot) this.#maybeIdle(slot);
        }
      }
    } catch (error) {
      if (this.#connectors.get(connector.name) === connector) void this.#withdrawAll(connector);
      throw error;
    } finally {
      this.#pendingServe.delete(connector);
    }
  }

  /** Stop serving a Connector on every connection. Best effort: a lost connection ends it anyway. */
  async withdraw(connector: PoolConnector): Promise<void> {
    if (this.#connectors.get(connector.name) !== connector) return;
    await this.#withdrawAll(connector);
  }

  /**
   * End every attachment with reason `closed`, release them and close the
   * connections. Later attaches are refused.
   */
  async close(): Promise<void> {
    if (this.#closed) return;
    this.#closed = true;
    const connectors = [...this.#connectors.values()];
    this.#connectors.clear();
    for (const connector of connectors) safely(() => connector.ended({ reason: "closed" }));
    const attachments = [...this.#wanted.values()];
    this.#wanted.clear();
    const detaches: Promise<unknown>[] = [];
    for (const attachment of attachments) {
      const slot = this.#where.get(attachment);
      this.#where.delete(attachment);
      if (slot && slot.held.get(attachment.sessionId) === attachment) {
        this.#release(slot, attachment.sessionId);
        detaches.push(
          slot.transport
            .request("session.detach", { sessionId: attachment.sessionId })
            .catch(() => undefined),
        );
      }
      safely(() => attachment.ended({ reason: "closed" }));
    }
    const grace = new AbortController();
    await Promise.race([
      Promise.allSettled(detaches),
      sleep(CLOSE_GRACE_MS, grace.signal).catch(() => undefined),
    ]);
    grace.abort();
    for (const slot of this.#slots) {
      clearTimeout(slot.idleTimer);
      slot.transport.close(1000, "client closed");
    }
    this.#slots.clear();
  }

  async #attach(attachment: PoolAttachment, attempts: number): Promise<AttachResult> {
    for (let failures = 0; ; ) {
      if (this.#wanted.get(attachment.sessionId) !== attachment)
        throw new TiDBLinkError("The Session connection was closed while attaching");
      let slot: Slot | undefined;
      try {
        slot = await this.#reserveSlot();
        const held = slot;
        let taken = false;
        // The Session's events may arrive with the answer: route them from the answer on.
        const take = (result: unknown) => {
          if (!isAttachResult(result) || this.#wanted.get(attachment.sessionId) !== attachment)
            return;
          taken = true;
          held.held.set(attachment.sessionId, attachment);
          this.#where.set(attachment, held);
          attachment.attached(result, held.transport);
        };
        const result = await this.#limited(() =>
          held.transport.request("session.attach", attachment.attachParams(), take),
        );
        if (!isAttachResult(result))
          throw new TiDBLinkError("session.attach returned an invalid result");
        if (!taken) {
          // Closed while the attach was in flight: release what it took.
          held.transport
            .request("session.detach", { sessionId: attachment.sessionId })
            .catch(() => undefined);
          throw new TiDBLinkError("The Session connection was closed while attaching");
        }
        return result;
      } catch (error) {
        failures += 1;
        if (
          error instanceof BackendRequestError &&
          slot &&
          (error.reason === "connection_full" || error.reason === "connection_draining")
        ) {
          // Try another connection at once; a core that refuses every one still ends the loop.
          if (error.reason === "connection_full") slot.full = true;
          else slot.draining = true;
          if (failures >= attempts) throw error;
          if (failures > 2) await sleep(backoff(failures));
          continue;
        }
        if (!isTransient(error) || failures >= attempts) throw error;
        await sleep(backoff(failures));
      } finally {
        if (slot) {
          slot.reserved -= 1;
          this.#maybeIdle(slot);
        }
      }
    }
  }

  /** Re-attach after a lost or draining connection, until it works or is given up. */
  async #recover(attachment: PoolAttachment, jitterMs: number): Promise<void> {
    try {
      if (jitterMs) await sleep(Math.round(Math.random() * jitterMs));
      await this.#attach(attachment, Number.POSITIVE_INFINITY);
    } catch (error) {
      if (this.#wanted.get(attachment.sessionId) !== attachment) return;
      this.#wanted.delete(attachment.sessionId);
      safely(() => attachment.ended({ reason: "error", error }));
    }
  }

  /** Whether new requests may go to this connection. */
  #serves(slot: Slot): boolean {
    return this.#slots.has(slot) && slot.transport.open && !slot.draining;
  }

  /** Declare a Connector on one connection; repeated calls share the declaration. */
  #declare(slot: Slot, connector: PoolConnector): Promise<string> {
    const existing = slot.serving.get(connector.name);
    if (existing) return existing;
    const declaration = slot.transport
      .request("connector.serve", connector.serveParams)
      .then((result) => {
        if (!isServeResult(result))
          throw new TiDBLinkError("connector.serve returned an invalid result");
        return result.connectorId;
      });
    slot.serving.set(connector.name, declaration);
    declaration.catch(() => {
      if (slot.serving.get(connector.name) === declaration) slot.serving.delete(connector.name);
    });
    return declaration;
  }

  /**
   * Declare a Connector on a further connection. A lost or draining
   * connection is replaced and declares it again; a transient refusal is
   * retried on the same connection; other failures are reported.
   */
  async #declareQuietly(slot: Slot, connector: PoolConnector): Promise<void> {
    for (let failures = 0; ; ) {
      try {
        await this.#declare(slot, connector);
        return;
      } catch (error) {
        if (this.#connectors.get(connector.name) !== connector) return;
        if (error instanceof BackendConnectionClosedError) return;
        if (error instanceof BackendRequestError && error.reason === "connection_draining") return;
        if (isTransient(error) && this.#serves(slot)) {
          // The connection stays open: serve it there again later.
          await sleep(backoff(++failures));
          if (this.#connectors.get(connector.name) !== connector || !this.#serves(slot)) return;
          continue;
        }
        if (!this.#pendingServe.has(connector)) safely(() => connector.report(error));
        return;
      }
    }
  }

  async #withdrawAll(connector: PoolConnector): Promise<void> {
    this.#connectors.delete(connector.name);
    const withdrawals: Promise<unknown>[] = [];
    for (const slot of this.#slots) {
      if (!slot.serving.delete(connector.name)) continue;
      if (slot.transport.open)
        withdrawals.push(
          slot.transport
            .request("connector.withdraw", { name: connector.name })
            .catch(() => undefined),
        );
    }
    await Promise.allSettled(withdrawals);
    for (const slot of this.#slots) this.#maybeIdle(slot);
  }

  /**
   * Keep one connection open while Connectors are served: reopen after the
   * last one was lost or started draining. The new connection declares them.
   */
  async #keepServing(): Promise<void> {
    if (this.#reopening) return;
    this.#reopening = true;
    try {
      for (let failures = 0; this.#connectors.size && !this.#closed; failures++) {
        if ([...this.#slots].some((slot) => this.#serves(slot))) return;
        try {
          await this.#openShared();
          return;
        } catch (error) {
          if (this.#closed) return;
          if (!isTransient(error)) {
            const connectors = [...this.#connectors.values()];
            this.#connectors.clear();
            for (const connector of connectors)
              safely(() => connector.ended({ reason: "error", error }));
            return;
          }
          await sleep(backoff(failures));
        }
      }
    } finally {
      this.#reopening = false;
    }
  }

  async #limited<T>(task: () => Promise<T>): Promise<T> {
    if (this.#attaching >= ATTACH_CONCURRENCY)
      await new Promise<void>((resolve) => this.#attachQueue.push(resolve));
    this.#attaching += 1;
    try {
      return await task();
    } finally {
      this.#attaching -= 1;
      this.#attachQueue.shift()?.();
    }
  }

  /** A connection with room for one more attachment, reserved for the caller. */
  async #reserveSlot(): Promise<Slot> {
    for (;;) {
      for (const slot of this.#slots)
        if (
          slot.transport.open &&
          !slot.draining &&
          !slot.full &&
          slot.held.size + slot.reserved < slot.transport.hello.maxSessions
        ) {
          // Reserve before yielding, so that concurrent attaches count each other.
          slot.reserved += 1;
          this.#cancelIdle(slot);
          return slot;
        }
      await this.#openShared();
    }
  }

  /** An open connection that takes new requests, opened if there is none. */
  async #servingSlot(): Promise<Slot> {
    for (const slot of this.#slots) if (this.#serves(slot)) return slot;
    return this.#openShared();
  }

  async #anySlot(): Promise<Slot> {
    let fallback: Slot | undefined;
    for (const slot of this.#slots) {
      if (!slot.transport.open) continue;
      if (!slot.draining) return slot;
      fallback ??= slot;
    }
    return fallback ?? (await this.#openShared());
  }

  /** One connection opens at a time; concurrent callers share it. */
  #openShared(): Promise<Slot> {
    if (this.#closed)
      return Promise.reject(new TiDBLinkError("This OrganizationClient is closed"));
    this.#opening ??= this.#open().finally(() => {
      this.#opening = undefined;
    });
    return this.#opening;
  }

  async #open(): Promise<Slot> {
    if (this.#openFailures) await sleep(backoff(this.#openFailures));
    let slot: Slot | undefined;
    let transport: BackendTransport;
    try {
      transport = await BackendTransport.open({
        url: this.#url,
        apiKey: this.#apiKey,
        WebSocket: this.#WebSocket,
        ...(this.#openTimeoutMs === undefined ? {} : { openTimeoutMs: this.#openTimeoutMs }),
        onNotification: (method, params, size) => {
          if (slot) this.#notify(slot, method, params, size);
        },
      });
    } catch (error) {
      this.#openFailures += 1;
      throw error;
    }
    this.#openFailures = 0;
    slot = {
      transport,
      held: new Map(),
      reserved: 0,
      full: false,
      draining: false,
      idleTimer: undefined,
      serving: new Map(),
    };
    if (this.#closed) {
      transport.close(1000, "client closed");
      throw new TiDBLinkError("This OrganizationClient is closed");
    }
    this.#slots.add(slot);
    const opened = slot;
    void transport.closed.then((error) => this.#lost(opened, error));
    for (const connector of this.#connectors.values()) void this.#declareQuietly(slot, connector);
    this.#maybeIdle(slot);
    return slot;
  }

  #notify(slot: Slot, method: string, params: Record<string, unknown>, size: number): void {
    if (method === "connection.drain") {
      const closeAfterMs = Number(params.closeAfterMs);
      this.#drain(slot, Number.isFinite(closeAfterMs) ? closeAfterMs : 0);
      return;
    }
    // A Connector call needs no attachment: route it by the Connector it names.
    if (method === "tool.call" && params.connector !== undefined) {
      const name =
        typeof params.connector === "object" && params.connector !== null
          ? (params.connector as { name?: unknown }).name
          : undefined;
      const connector = typeof name === "string" ? this.#connectors.get(name) : undefined;
      // Withdrawn here: unclaimed, the call ends as not performed.
      if (connector) safely(() => connector.call(params, slot.transport));
      return;
    }
    const sessionId = params.sessionId;
    if (typeof sessionId !== "string") return;
    const attachment = slot.held.get(sessionId);
    if (!attachment) return;
    if (method === "session.detached") {
      this.#release(slot, sessionId);
      this.#where.delete(attachment);
      if (this.#wanted.get(sessionId) === attachment) this.#wanted.delete(sessionId);
      safely(() =>
        attachment.ended({
          reason: params.reason === "session_deleted" ? "session_deleted" : "superseded",
        }),
      );
      this.#maybeIdle(slot);
      return;
    }
    safely(() => attachment.notify(method, params, slot.transport, size));
  }

  /** Move every attachment to another connection, then close this one. */
  #drain(slot: Slot, closeAfterMs: number): void {
    if (slot.draining && slot.held.size === 0) return;
    slot.draining = true;
    const deadline = Date.now() + closeAfterMs;
    const moving = [...slot.held.values()];
    // Stop routing now: each re-attach resumes from what its Session processed.
    slot.held.clear();
    const moves: Promise<void>[] = moving.map((attachment) => {
      this.#where.delete(attachment);
      return this.#recover(attachment, 0);
    });
    // Serve the Connectors on another connection before this one closes.
    moves.push(this.#keepServing());
    void (async () => {
      await Promise.allSettled(moves);
      // Let claims and results in flight finish, then close before the core does.
      while (slot.transport.open && slot.transport.pendingRequests > 0 && Date.now() < deadline)
        await sleep(50);
      slot.transport.close(1000, "drained");
    })();
  }

  #lost(slot: Slot, error: BackendConnectionClosedError): void {
    this.#slots.delete(slot);
    clearTimeout(slot.idleTimer);
    const held = [...slot.held.values()];
    slot.held.clear();
    for (const attachment of held) {
      this.#where.delete(attachment);
      if (this.#wanted.get(attachment.sessionId) !== attachment) continue;
      if (error.retryable && !this.#closed) {
        void this.#recover(attachment, RECOVERY_JITTER_MS);
      } else {
        this.#wanted.delete(attachment.sessionId);
        safely(() => attachment.ended({ reason: "error", error }));
      }
    }
    if (!this.#connectors.size || this.#closed) return;
    if (error.retryable) {
      void this.#keepServing();
    } else if (![...this.#slots].some((other) => this.#serves(other))) {
      // The Key no longer works: no connection can serve them.
      const connectors = [...this.#connectors.values()];
      this.#connectors.clear();
      for (const connector of connectors) safely(() => connector.ended({ reason: "error", error }));
    }
  }

  #release(slot: Slot, sessionId: string): void {
    slot.held.delete(sessionId);
    slot.full = false;
  }

  #cancelIdle(slot: Slot): void {
    clearTimeout(slot.idleTimer);
    slot.idleTimer = undefined;
  }

  /** The only connection serving the Connectors stays open while any is served. */
  #keepsServing(slot: Slot): boolean {
    return (
      this.#connectors.size > 0 &&
      this.#serves(slot) &&
      ![...this.#slots].some((other) => other !== slot && this.#serves(other))
    );
  }

  #maybeIdle(slot: Slot): void {
    if (slot.idleTimer || !this.#slots.has(slot) || slot.held.size || slot.reserved) return;
    if (this.#keepsServing(slot)) return;
    slot.idleTimer = setTimeout(() => {
      slot.idleTimer = undefined;
      if (!this.#slots.has(slot) || slot.held.size || slot.reserved) return;
      if (this.#keepsServing(slot)) return;
      // Claims and results may still be in flight; look again later.
      if (slot.transport.pendingRequests) return this.#maybeIdle(slot);
      this.#slots.delete(slot);
      slot.transport.close(1000, "idle");
    }, this.#idleCloseMs);
  }
}

function safely(callback: () => void): void {
  try {
    callback();
  } catch (error) {
    queueMicrotask(() => {
      throw error;
    });
  }
}
