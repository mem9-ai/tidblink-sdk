// Features TiDB Link enables only for some Organizations. Each may change or
// be removed in any release; the root entry never imports this one.
export { FeatureNotEnabledError } from "./errors.js";
export type { GatedFeature } from "./errors.js";

export { agents, Agents } from "./agents.js";
export type {
  Agent,
  AgentRecord,
  AgentPage,
  AgentSessionDefaults,
  CreateAgentInput,
  UpdateAgentInput,
  UpdateAgentOptions,
  CreateAgentSessionOptions,
} from "./agents.js";
