"""Reads of one End User's usage (``usage.ts`` in the TypeScript SDK).

The SDK returns the service's values unchanged.
"""

from __future__ import annotations

from typing import Literal, NotRequired, TypedDict, Union

from ._http import query_string, require_user_id, segment, template_filter
from .history import BillingTag

BillingUsageFactKind = Literal["llm", "sandbox", "web_search"]
BillingCostBearer = Literal["platform", "customer", "unknown"]
BillingPrincipalBucket = Literal[
    "human", "user_api_key", "organization_machine", "organization_user"
]
BillingAttributionKind = Literal["user_level", "system", "unattributed"]
BillingLlmRequestPurpose = Literal[
    "agent_turn", "compaction", "auto_title", "manual_title", "asr", "voice_realtime"
]
LlmPricingStatus = Literal["estimated", "unknown"]
LlmPricingContextTier = Literal["short", "long", "all_context", "unknown"]
SandboxUsageStatus = Literal["running", "closed", "close_failed", "abandoned"]
BillingUsageEvidenceStatus = Literal["complete", "partial", "missing", "unsupported"]
BillingValuationStatus = Literal["priced", "unpriced"]
BillingUsageSeriesInterval = Literal["hour", "day", "week", "month"]
SandboxProvider = Literal["e2b", "tencent_ags", "run9"]
_CompletenessStatus = Literal["complete", "incomplete"]


class BillingValuationSummary(TypedDict):
    status: _CompletenessStatus
    pricedUsageCount: int
    unpricedUsageCount: int
    pricedCostMicros: int


class BillingValuationByKind(TypedDict, total=False):
    llm: BillingValuationSummary
    sandbox: BillingValuationSummary
    web_search: BillingValuationSummary


class BillingValuationBreakdown(BillingValuationSummary):
    byKind: BillingValuationByKind


class BillingFactCompletenessCounters(TypedDict):
    status: _CompletenessStatus
    projectedFactCount: int
    pendingIntentCount: int
    indeterminateIntentCount: int
    failedIntentCount: int


class BillingFactCompletenessByKind(TypedDict, total=False):
    llm: BillingFactCompletenessCounters
    sandbox: BillingFactCompletenessCounters
    web_search: BillingFactCompletenessCounters


class BillingFactCompleteness(BillingFactCompletenessCounters):
    byKind: BillingFactCompletenessByKind


class BillingBearerCostTotals(TypedDict):
    totalCostMicros: int
    llmCostMicros: int
    sandboxCostMicros: int
    webSearchCostMicros: int


class BillingCostByBearer(TypedDict):
    platform: BillingBearerCostTotals
    customer: BillingBearerCostTotals
    unknown: BillingBearerCostTotals


class BillingAttributionCounters(TypedDict):
    status: _CompletenessStatus
    classifiedUsageCount: int
    userLevelUsageCount: int
    systemUsageCount: int
    unattributedUsageCount: int


class BillingAttributionByKind(TypedDict, total=False):
    llm: BillingAttributionCounters
    sandbox: BillingAttributionCounters
    web_search: BillingAttributionCounters


class BillingAttributionBreakdown(BillingAttributionCounters):
    byKind: BillingAttributionByKind


class UnpricedLlmTokens(TypedDict):
    inputTokens: int
    cachedInputTokens: int
    outputTokens: int


class UsageTotals(TypedDict):
    #: The single currency of this deployment.
    currency: Literal["USD", "CNY"]
    #: Platform-borne priced cost; other bearers remain in ``costByBearer``.
    totalCostMicros: int
    llmCostMicros: int
    sandboxCostMicros: int
    webSearchCostMicros: int
    costByBearer: BillingCostByBearer
    organizationMachineCostMicros: int
    llmRequestCount: int
    unknownLlmRequestCount: int
    sandboxUsageCount: int
    webSearchRequestCount: int
    unsettledCount: int
    inputTokens: int
    cachedInputTokens: int
    outputTokens: int
    unpricedLlmTokens: UnpricedLlmTokens
    sandboxDurationSeconds: float
    valuation: BillingValuationBreakdown
    attribution: BillingAttributionBreakdown


class LlmUsageAggregate(TypedDict):
    costBearer: BillingCostBearer
    provider: str
    model: str
    requestPurpose: BillingLlmRequestPurpose
    principalBucket: BillingPrincipalBucket
    requestCount: int
    unknownRequestCount: int
    costMicros: int
    inputTokens: int
    cachedInputTokens: int
    outputTokens: int
    valuation: BillingValuationSummary


class SandboxUsageAggregate(TypedDict):
    costBearer: BillingCostBearer
    provider: SandboxProvider
    profileId: str
    specKey: str
    principalBucket: BillingPrincipalBucket
    usageCount: int
    settledUsageCount: int
    unsettledCount: int
    costMicros: int
    durationSeconds: float
    valuation: BillingValuationSummary


class WebSearchUsageAggregate(TypedDict):
    costBearer: BillingCostBearer
    provider: Literal["exa"]
    searchType: Literal["auto"]
    principalBucket: BillingPrincipalBucket
    requestCount: int
    requestedResultCount: int
    returnedResultCount: int
    costMicros: int
    valuation: BillingValuationSummary


# ``from`` is a Python keyword, so this record uses the functional syntax.
BillingUsageSeriesRange = TypedDict(
    "BillingUsageSeriesRange",
    {"from": str, "to": str, "interval": BillingUsageSeriesInterval},
)


class _BillingUsageSeriesBreakdownCommon(TypedDict):
    provider: str
    costMicros: int
    truncatedCombinations: NotRequired[int]


class BillingUsageSeriesLlmUsage(TypedDict):
    inputTokens: int
    cachedInputTokens: int
    outputTokens: int


class BillingUsageSeriesLlmBreakdown(_BillingUsageSeriesBreakdownCommon):
    kind: Literal["llm"]
    model: NotRequired[str]
    usage: BillingUsageSeriesLlmUsage


class BillingUsageSeriesSandboxUsage(TypedDict):
    durationSeconds: float
    usageCount: int


class BillingUsageSeriesSandboxBreakdown(_BillingUsageSeriesBreakdownCommon):
    kind: Literal["sandbox"]
    usage: BillingUsageSeriesSandboxUsage


class BillingUsageSeriesWebSearchUsage(TypedDict):
    requestCount: int


class BillingUsageSeriesWebSearchBreakdown(_BillingUsageSeriesBreakdownCommon):
    kind: Literal["web_search"]
    usage: BillingUsageSeriesWebSearchUsage


BillingUsageSeriesBreakdown = Union[
    BillingUsageSeriesLlmBreakdown,
    BillingUsageSeriesSandboxBreakdown,
    BillingUsageSeriesWebSearchBreakdown,
]


class BillingUsageSeriesBreakdownGroups(TypedDict):
    platform: list[BillingUsageSeriesBreakdown]
    customer: list[BillingUsageSeriesBreakdown]
    unknown: list[BillingUsageSeriesBreakdown]


class BillingUsageSeriesPoint(TypedDict):
    bucketStart: str
    costMicros: int
    llmCostMicros: int
    sandboxCostMicros: int
    webSearchCostMicros: int
    unknownLlmRequestCount: int
    unsettledCount: int
    breakdown: BillingUsageSeriesBreakdownGroups
    valuation: BillingValuationSummary


class LlmUsageAggregates(TypedDict):
    aggregates: list[LlmUsageAggregate]


class SandboxUsageAggregates(TypedDict):
    aggregates: list[SandboxUsageAggregate]


class WebSearchUsageAggregates(TypedDict):
    aggregates: list[WebSearchUsageAggregate]


class OrganizationUserUsageSummary(TypedDict):
    range: BillingUsageSeriesRange
    factCompleteness: BillingFactCompleteness
    totals: UsageTotals
    llm: LlmUsageAggregates
    sandbox: SandboxUsageAggregates
    webSearch: WebSearchUsageAggregates
    series: list[BillingUsageSeriesPoint]


class _OrganizationUsageCommon(TypedDict):
    id: str
    occurredAt: str
    sessionId: str
    agentRunId: str | None
    #: The Template admitted for the usage's Turn; None when it has none.
    agentTemplateId: str | None
    attributionKind: BillingAttributionKind
    costMicros: int | None
    currency: str | None
    pricingVersion: str
    pricingEffectiveAt: str
    valuationStatus: BillingValuationStatus
    valuationReason: str | None
    rateCardDigest: str | None
    billingTag: BillingTag | None


class OrganizationLlmUsage(_OrganizationUsageCommon):
    kind: Literal["llm"]
    #: None for ``voice_realtime`` usage.
    turnId: str | None
    #: The Realtime connection of ``voice_realtime`` usage; None otherwise.
    realtimeConnectionId: str | None
    #: For ``voice_realtime`` usage, whether its Realtime connection was still
    #: live when read; a live connection reports usage in interim items. None otherwise.
    realtimeConnectionLive: bool | None
    operationId: str | None
    requestPurpose: BillingLlmRequestPurpose
    provider: str
    model: str
    observedResponseModel: str | None
    inputTokens: int
    cachedInputTokens: int
    uncachedInputTokens: int
    cacheReadTokens: int
    cacheWrite5mTokens: int
    cacheWrite1hTokens: int
    cacheWriteUnspecifiedTokens: int
    outputTokens: int
    inputCostMicros: int | None
    cachedInputCostMicros: int | None
    cacheWrite5mCostMicros: int | None
    cacheWrite1hCostMicros: int | None
    cacheWriteUnspecifiedCostMicros: int | None
    outputCostMicros: int | None
    pricingStatus: LlmPricingStatus
    estimateBasis: Literal["public_list_placeholder", "contract"] | None
    contextTier: LlmPricingContextTier


class OrganizationSandboxUsage(_OrganizationUsageCommon):
    kind: Literal["sandbox"]
    turnId: str
    realtimeConnectionId: NotRequired[str | None]
    provider: SandboxProvider
    profileId: str
    specKey: str
    startedAt: str
    endedAt: str | None
    durationSeconds: float | None
    status: SandboxUsageStatus


class OrganizationWebSearchUsage(_OrganizationUsageCommon):
    kind: Literal["web_search"]
    turnId: str
    realtimeConnectionId: NotRequired[str | None]
    operationId: str
    provider: Literal["exa"]
    searchType: Literal["auto"]
    requestCount: Literal[1]
    requestedResultCount: int
    returnedResultCount: int | None
    contentTextRequested: Literal[True]
    requestOutcome: Literal["succeeded", "failed", "aborted"]
    usageEvidenceStatus: BillingUsageEvidenceStatus


OrganizationUsageItem = Union[
    OrganizationLlmUsage, OrganizationSandboxUsage, OrganizationWebSearchUsage
]


class OrganizationUserUsageDetails(TypedDict):
    items: list[OrganizationUsageItem]
    #: None ends the stream; pass it back as ``cursor`` with the original filters.
    nextCursor: str | None
    factCompleteness: BillingFactCompleteness


def user_usage_path(user_id: str, from_: object, to: object) -> str:
    """The usage path of an explicit User, given an exact ``from_``/``to`` range."""
    require_user_id(user_id, "Organization usage requires an explicit User ID")
    if not isinstance(from_, str) or not isinstance(to, str):
        raise TypeError("Organization usage requires from_ and to UTC timestamps")
    if not from_ or not to:
        raise ValueError("Organization usage requires from_ and to UTC timestamps")
    return f"/users/{segment(user_id)}/billing/usage"


def usage_query(
    *,
    from_: str,
    to: str,
    template_id: str | None = None,
    tag_key: str | None = None,
    tag_value: str | None = None,
    **extra: str | int | None,
) -> str:
    """The query of a usage read: the range, ``extra`` wire parameters and the filters."""
    for name, value in (("tag_key", tag_key), ("tag_value", tag_value), *extra.items()):
        if value is None:
            continue
        if name == "limit":
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError("limit must be an integer")
        elif not isinstance(value, str):
            raise TypeError(f"{name} must be text")
    return query_string(
        {
            "from": from_,
            "to": to,
            **extra,
            **template_filter(template_id),
            "tagKey": tag_key,
            "tagValue": tag_value,
        }
    )
