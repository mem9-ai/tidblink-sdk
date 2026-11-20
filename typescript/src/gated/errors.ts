import { TiDBLinkApiError } from "../errors.js";

/** A gated feature, as named on `FeatureNotEnabledError.feature`. */
export type GatedFeature = "agents";

/**
 * The service refused a gated feature because the Organization is not enabled
 * for it. `status`, `code` and `requestId` are the service's own; enabling the
 * Organization makes the same request succeed.
 */
export class FeatureNotEnabledError extends TiDBLinkApiError {
  readonly feature: GatedFeature;

  constructor(input: ConstructorParameters<typeof TiDBLinkApiError>[0] & {
    feature: GatedFeature;
  }) {
    const { feature, ...rest } = input;
    super(rest);
    this.feature = feature;
  }
}

/** The service's refusal code for each gated feature. Change it here when the service does. */
const REFUSALS = new Map<string, { feature: GatedFeature; label: string }>([
  ["agent_preview_not_enabled", { feature: "agents", label: "Agents" }],
]);

/** Rethrow a gated feature's 403 refusal as `FeatureNotEnabledError`, and anything else unchanged. */
export const rethrowGated = (error: unknown): never => {
  if (
    !(error instanceof TiDBLinkApiError) ||
    error instanceof FeatureNotEnabledError ||
    error.status !== 403
  )
    throw error;
  const refusal = REFUSALS.get(error.code);
  if (!refusal) throw error;
  throw new FeatureNotEnabledError({
    status: error.status,
    code: error.code,
    message: `${refusal.label} are not enabled for this Organization; they are available only to Organizations TiDB Link has enabled`,
    feature: refusal.feature,
    ...(error.requestId === undefined ? {} : { requestId: error.requestId }),
    ...(error.details === undefined ? {} : { details: error.details }),
    ...(error.retryAfter === undefined ? {} : { retryAfter: error.retryAfter }),
  });
};
