import { validateClientTools } from "../client-tools.js";
import { OutcomeUnknownError } from "../errors.js";
import {
  HttpClient,
  paginationQuery,
  requestOptions,
  requireCreationKey,
  requireEtag,
  requireKnownKeys,
  requireTemplateId,
  type HttpResult,
  type ListOptions,
} from "../http.js";
import type { RequestOptions } from "../organization.js";
import {
  Session,
  type CreateSessionOptions,
  type SessionRecord,
} from "../session.js";
import { endUserHttp, type EndUserClient, type SessionPage } from "../user.js";
import { rethrowGated } from "./errors.js";

/**
 * The Session creation settings an Agent stores: `createSession` options
 * without `idempotencyKey`, `tools` and request options. Pass them back to
 * `user.createSession({ ...agent.sessionDefaults, idempotencyKey })` to create
 * the same Session without the Agent.
 */
export type AgentSessionDefaults = Omit<
  CreateSessionOptions,
  "idempotencyKey" | "tools" | "signal" | "requestId"
>;

/** An Agent as listed: a named preset of Session creation settings. */
export interface AgentRecord {
  agentId: string;
  /** The End User who owns the Agent. */
  userId: string;
  name: string;
  /** Stored as accepted; Session creation checks them again. */
  sessionDefaults: AgentSessionDefaults;
  createdAt: string;
  updatedAt: string;
}

/** An Agent read by itself, with the revision `update` needs. */
export interface Agent extends AgentRecord {
  /** Pass this exact value as `ifMatch` to update the Agent. */
  etag: string;
}

export interface AgentPage {
  /** Newest first. List entries carry no `etag`; `get` one to update it. */
  agents: AgentRecord[];
  hasMore: boolean;
  nextCursor: string | null;
}

export interface CreateAgentInput extends RequestOptions {
  /** 1-100 characters. */
  name: string;
  sessionDefaults: AgentSessionDefaults;
  /** Repeating the key and input returns the Agent that key created. */
  idempotencyKey: string;
}

export interface UpdateAgentInput {
  name?: string;
  /** Replaces the stored defaults as a whole, so send them complete. */
  sessionDefaults?: AgentSessionDefaults;
}

export interface UpdateAgentOptions extends RequestOptions {
  /** The `etag` of the Agent you read. A stale value is refused with ConflictError. */
  ifMatch: string;
}

/** The per-Session choices; the Agent supplies the other settings. */
export type CreateAgentSessionOptions = Pick<
  CreateSessionOptions,
  | "idempotencyKey"
  | "model"
  | "realtimeModel"
  | "billingTag"
  | "tools"
  | "signal"
  | "requestId"
> & {
  /** Overrides the Agent's voice; `null` drops it so the Session speaks the default voice. */
  realtimeVoice?: string | null;
};

type WireSessionDefaults = Omit<AgentSessionDefaults, "templateId"> & {
  agentTemplateId: string;
};
type WireAgent = Omit<AgentRecord, "sessionDefaults"> & {
  sessionDefaults: WireSessionDefaults;
};

const SESSION_DEFAULT_KEYS = [
  "templateId",
  "model",
  "realtimeModel",
  "realtimeVoice",
  "billingTag",
  "additionalInstructions",
  "sandbox",
  "acknowledgeSessionRecall",
] as const satisfies readonly (keyof AgentSessionDefaults)[];

const toWire = (
  defaults: AgentSessionDefaults,
  label: string,
): WireSessionDefaults => {
  requireKnownKeys(defaults, SESSION_DEFAULT_KEYS, `${label} sessionDefaults`);
  requireTemplateId(defaults.templateId, label);
  const { templateId, ...rest } = defaults;
  return { agentTemplateId: templateId, ...rest };
};

const fromWire = (agent: WireAgent): AgentRecord => {
  const { agentTemplateId, ...rest } = agent.sessionDefaults;
  return {
    ...agent,
    sessionDefaults: { templateId: agentTemplateId, ...rest },
  };
};

const requireAgentId = (agentId: unknown): string => {
  if (typeof agentId !== "string" || !agentId)
    throw new TypeError("An Agent ID is required");
  return encodeURIComponent(agentId);
};

const requireName = (name: unknown, label: string): void => {
  if (typeof name !== "string" || !name.trim())
    throw new TypeError(`${label} requires a name`);
};

const request = ({
  signal,
  requestId,
}: {
  signal?: AbortSignal | undefined;
  requestId?: string | undefined;
}) => ({
  ...(signal ? { signal } : {}),
  ...(requestId ? { requestId } : {}),
});

/** Agents with their ETag, or an uncertain outcome when a mutation lost either. */
const agentReceipt = (
  result: HttpResult<{ agent?: WireAgent }>,
  missing: string,
): Agent => {
  const agent = result.data?.agent;
  if (typeof agent?.agentId !== "string" || !agent.agentId || !result.etag)
    throw new OutcomeUnknownError(missing, { requestId: result.requestId });
  return { ...fromWire(agent), etag: result.etag };
};

/**
 * One End User's Agents: named presets of Session creation settings that
 * create ordinary Sessions. Available only to Organizations enabled for
 * Agents; others receive `FeatureNotEnabledError`. Get one with
 * `agents(user)`.
 */
export class Agents {
  readonly #http: HttpClient;

  constructor(http: HttpClient) {
    this.#http = http;
  }

  /** Create an Agent. Its Template must currently be selectable. */
  async create(input: CreateAgentInput): Promise<Agent> {
    requireKnownKeys(
      input,
      ["name", "sessionDefaults", "idempotencyKey", "signal", "requestId"],
      "Agent creation",
    );
    requireName(input.name, "Agent creation");
    requireCreationKey(input.idempotencyKey, "Agent creation");
    const body = {
      name: input.name,
      sessionDefaults: toWire(input.sessionDefaults, "Agent creation"),
    };
    const result = await this.#http
      .postResponse<{ agent?: WireAgent }>("/agents", body, {
        idempotencyKey: input.idempotencyKey,
        idempotent: true,
        outcomeUnknown: true,
        ...request(input),
      })
      .catch(rethrowGated);
    return agentReceipt(
      result,
      "Agent creation returned no usable Agent; recover with the same creation key",
    );
  }

  /** The End User's Agents, newest first. */
  async list(options?: ListOptions): Promise<AgentPage> {
    const page = await this.#http
      .get<{
        agents: WireAgent[];
        hasMore: boolean;
        nextCursor: string | null;
      }>(`/agents${paginationQuery(options)}`, options)
      .catch(rethrowGated);
    return { ...page, agents: page.agents.map(fromWire) };
  }

  async get(agentId: string, options?: RequestOptions): Promise<Agent> {
    const path = `/agents/${requireAgentId(agentId)}`;
    const result = await this.#http
      .getResponse<{ agent: WireAgent }>(path, requestOptions(options))
      .catch(rethrowGated);
    return { ...fromWire(result.data.agent), etag: requireEtag(result) };
  }

  /**
   * Change the name or the Session defaults. Sessions created afterwards use
   * the change; existing Sessions keep their configuration.
   */
  async update(
    agentId: string,
    input: UpdateAgentInput,
    options: UpdateAgentOptions,
  ): Promise<Agent> {
    const path = `/agents/${requireAgentId(agentId)}`;
    requireKnownKeys(input, ["name", "sessionDefaults"], "Agent update");
    if (input.name !== undefined) requireName(input.name, "Agent update");
    requireKnownKeys(
      options,
      ["ifMatch", "signal", "requestId"],
      "Agent update options",
    );
    if (typeof options.ifMatch !== "string" || !options.ifMatch)
      throw new TypeError("Agent update requires ifMatch");
    const body = {
      ...(input.name === undefined ? {} : { name: input.name }),
      ...(input.sessionDefaults === undefined
        ? {}
        : { sessionDefaults: toWire(input.sessionDefaults, "Agent update") }),
    };
    const result = await this.#http
      .patchResponse<{ agent?: WireAgent }>(path, body, {
        ifMatch: options.ifMatch,
        outcomeUnknown: true,
        ...request(options),
      })
      .catch(rethrowGated);
    return agentReceipt(
      result,
      "Agent update returned no usable Agent; read the Agent before changing it again",
    );
  }

  /** Delete the Agent. Sessions created through it remain ordinary Sessions. */
  async delete(agentId: string, options?: RequestOptions): Promise<void> {
    const path = `/agents/${requireAgentId(agentId)}`;
    await this.#http
      .deleteResponse(path, { ...requestOptions(options), outcomeUnknown: true })
      .catch(rethrowGated);
  }

  /**
   * Create an ordinary Session from the Agent's defaults and these choices.
   * The key shares the namespace of `user.createSession`: a retry returns the
   * original Session even after the Agent changed.
   */
  async createSession(
    agentId: string,
    options: CreateAgentSessionOptions,
  ): Promise<Session> {
    const path = `/agents/${requireAgentId(agentId)}/sessions`;
    requireKnownKeys(
      options,
      [
        "idempotencyKey",
        "model",
        "realtimeModel",
        "realtimeVoice",
        "billingTag",
        "tools",
        "signal",
        "requestId",
      ],
      "Agent Session options",
    );
    requireCreationKey(options.idempotencyKey);
    if (options.tools !== undefined) validateClientTools(options.tools);
    const { idempotencyKey, signal, requestId, ...body } = options;
    const result = await this.#http
      .postResponse<{ session?: SessionRecord }>(path, body, {
        idempotencyKey,
        idempotent: true,
        outcomeUnknown: true,
        ...request({ signal, requestId }),
      })
      .catch(rethrowGated);
    const record = result.data?.session;
    if (typeof record?.sessionId !== "string" || !record.sessionId)
      throw new OutcomeUnknownError(
        "Session creation returned no usable Session; recover with the same creation key",
        { requestId: result.requestId },
      );
    if (!result.etag)
      throw new OutcomeUnknownError(
        "Session creation returned no model ETag; recover with the same creation key",
        { requestId: result.requestId },
      );
    return new Session(this.#http, record.sessionId, record, result.etag);
  }

  /** The Sessions created through the Agent that still exist, newest first. */
  async listSessions(
    agentId: string,
    options?: ListOptions,
  ): Promise<SessionPage> {
    const path = `/agents/${requireAgentId(agentId)}/sessions`;
    const page = await this.#http
      .get<{
        sessions: SessionRecord[];
        hasMore: boolean;
        nextCursor: string | null;
      }>(`${path}${paginationQuery(options)}`, options)
      .catch(rethrowGated);
    return {
      ...page,
      sessions: page.sessions.map(
        (record) => new Session(this.#http, record.sessionId, record),
      ),
    };
  }
}

/**
 * The Agents of `user`. Sends no request: an Organization that is not enabled
 * for Agents learns so from the first call, as `FeatureNotEnabledError`.
 */
export const agents = (user: EndUserClient): Agents =>
  new Agents(endUserHttp(user));
