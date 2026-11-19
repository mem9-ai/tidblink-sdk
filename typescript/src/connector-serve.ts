import type { BackendConnectionPool, PoolConnector, ServingEnd } from "./backend-pool.js";
import { ClientToolExecutor } from "./client-tool-executor.js";
import { validateClientTools } from "./client-tools.js";
import { requireKnownKeys } from "./http.js";
import type { ClientToolHandlers } from "./session-connection.js";

export interface ServeConnectorOptions {
  /**
   * The Connector's name, unique in the Organization: lowercase letters and
   * digits joined by single underscores, 1 to 29 characters. Serving a name
   * the Organization has no Connector for creates one.
   */
  name: string;
  /**
   * The functions this process serves, by name, written like
   * `session.connect({ tools })`. Function names follow the Connector name
   * rule; the model sees each as `mcp_<name>__<function>`.
   */
  tools: ClientToolHandlers;
  /**
   * Errors that do not end serving: a declaration a connection refused, an
   * invalid call, a result the core did not accept. Without it they are
   * rethrown asynchronously, which by default ends a Node.js process.
   */
  onError?: (error: unknown) => void;
}

/** Why a served Connector stopped being served. */
export type ServedConnectorEnd =
  /** `close()` or `OrganizationClient.close()`. */
  | { reason: "closed" }
  /** No connection could serve it any more, for example after the Organization API Key was revoked. */
  | { reason: "error"; error: unknown };

/** The platform-reserved argument through which the model asks not to wait. */
const ASYNC_ARGUMENT = "async";
const NAME = /^[a-z0-9]+(_[a-z0-9]+)*$/;
const NAME_RULE = "lowercase letters and digits joined by single underscores, 1 to 29 characters";

const requireName = (value: unknown, what: string): void => {
  if (typeof value !== "string" || value.length > 29 || !NAME.test(value))
    throw new TypeError(`${what} must be ${NAME_RULE}`);
};

/** Check `serveConnector()` options and turn its handlers into declarations. */
export function connectorDeclarations(
  options: ServeConnectorOptions,
): Record<string, unknown>[] {
  if (typeof options !== "object" || options === null)
    throw new TypeError("serveConnector requires options");
  requireKnownKeys(options, ["name", "tools", "onError"], "serveConnector options");
  requireName(options.name, "The Connector name");
  if (options.onError !== undefined && typeof options.onError !== "function")
    throw new TypeError("onError must be a function");
  if (typeof options.tools !== "object" || options.tools === null || Array.isArray(options.tools))
    throw new TypeError("tools must map each Connector function name to its handler");
  const declarations = Object.entries(options.tools).map(([name, handler]) => {
    requireName(name, `Connector function name ${JSON.stringify(name)}`);
    requireKnownKeys(handler, ["description", "parameters", "run"], `Connector function ${name}`);
    if (typeof handler.run !== "function")
      throw new TypeError(`Connector function ${name} requires a run function`);
    if (typeof handler.description === "string" && handler.description.length > 4096)
      throw new TypeError(`The description of Connector function ${name} exceeds 4096 characters`);
    const parameters = handler.parameters as Record<string, unknown> | undefined;
    if (typeof parameters === "object" && parameters !== null) {
      if (parameters.type !== "object")
        throw new TypeError(`The parameters of Connector function ${name} must have type "object"`);
      const properties = parameters.properties;
      if (
        (typeof properties === "object" &&
          properties !== null &&
          Object.hasOwn(properties, ASYNC_ARGUMENT)) ||
        (Array.isArray(parameters.required) && parameters.required.includes(ASYNC_ARGUMENT))
      )
        throw new TypeError(
          `The parameters of Connector function ${name} may not define or require the platform-reserved "async" property`,
        );
    }
    return {
      type: "function" as const,
      name,
      description: handler.description,
      parameters: handler.parameters,
    };
  });
  if (declarations.length > 64)
    throw new TypeError("A Connector serves at most 64 functions");
  validateClientTools(declarations as never);
  return declarations;
}

/**
 * A Connector this process serves over its Backend Connections. Its calls can
 * come from any Session whose Template references `connectorId`, and run on
 * the connection the core sends them to. It is served until `close()`, or
 * until `closed` reports why it ended.
 */
export class ServedConnector {
  readonly name: string;
  /** The Organization's Connector with this name; Templates reference it as `tools.mcp[].serverId`. */
  readonly connectorId: string;
  /** Resolves once, with why serving ended. */
  readonly closed: Promise<ServedConnectorEnd>;
  readonly #close: () => Promise<void>;

  /** @internal Use `organization.serveConnector()`. */
  constructor(input: {
    name: string;
    connectorId: string;
    closed: Promise<ServedConnectorEnd>;
    close: () => Promise<void>;
  }) {
    this.name = input.name;
    this.connectorId = input.connectorId;
    this.closed = input.closed;
    this.#close = input.close;
  }

  /**
   * Stop serving on every connection. Calls published later find no
   * connection and end as not performed; calls already running finish and
   * their results are still submitted.
   */
  close(): Promise<void> {
    return this.#close();
  }
}

/** Serve a Connector; resolves once one connection serves it. */
export async function serveConnector(
  pool: BackendConnectionPool,
  options: ServeConnectorOptions,
): Promise<ServedConnector> {
  const tools = connectorDeclarations(options);
  const report = (error: unknown): void => {
    if (options.onError) {
      try {
        options.onError(error);
        return;
      } catch (thrown) {
        error = thrown;
      }
    }
    queueMicrotask(() => {
      throw error;
    });
  };
  let settle!: (end: ServedConnectorEnd) => void;
  let ended = false;
  const closed = new Promise<ServedConnectorEnd>((resolve) => {
    settle = resolve;
  });
  const end = (value: ServingEnd): void => {
    if (ended) return;
    ended = true;
    settle(value);
  };
  const executor = new ClientToolExecutor({
    pool,
    source: { connectorName: options.name },
    handlers: options.tools,
    report,
  });
  const registration: PoolConnector = {
    name: options.name,
    serveParams: { name: options.name, tools },
    call: (params, link) => executor.receive(params, link),
    report,
    ended: end,
  };
  const connectorId = await pool.serve(registration);
  return new ServedConnector({
    name: options.name,
    connectorId,
    closed,
    close: async () => {
      if (ended) return;
      end({ reason: "closed" });
      await pool.withdraw(registration);
    },
  });
}
