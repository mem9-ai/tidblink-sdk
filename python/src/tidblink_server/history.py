"""Saved conversation history: interactions, Turns and the timeline.

Records keep the service's field names; see ``history.ts`` in the TypeScript SDK.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, NotRequired, TypedDict

from ._http import HttpClient, pagination_query, query_string, segment
from .errors import TiDBLinkError

TurnStatus = Literal["running", "succeeded", "failed", "interrupted"]
RealtimeResponseKind = Literal[
    "chat", "task_ack", "task_progress", "task_result", "clarify", "device_ack"
]
OperationStatus = Literal["running", "succeeded", "failed", "interrupted", "unknown_result"]


class BillingTag(TypedDict):
    key: str
    value: str


class MessageRecord(TypedDict):
    id: str
    sessionId: str
    turnId: str
    role: Literal["user", "assistant"]
    content: str
    createdAt: str
    inputContext: NotRequired[dict[str, Any]]
    realtime: NotRequired[dict[str, Any]]


class TurnUserFile(TypedDict):
    turnId: str
    sessionId: str
    organizationId: str
    userFileId: str
    ordinal: int
    originalName: str
    safeName: str
    drivePath: str
    byteSize: int
    sha256: str
    checksumVerification: Literal["whole", "part_only"] | None
    contentType: str | None
    createdAt: str


class OperationRecord(TypedDict):
    id: str
    sessionId: str
    turnId: str
    scope: dict[str, str]
    kind: str
    backend: str
    targetKind: str
    mediator: Literal["aos"]
    status: OperationStatus
    input: Any
    output: NotRequired[Any]
    resultPointer: dict[str, Any]
    effectSummary: NotRequired[dict[str, Any] | None]
    compensatesOperationId: NotRequired[str | None]
    source: NotRequired[dict[str, Any]]
    label: NotRequired[str]
    category: NotRequired[Literal["tool_call", "skill_read"]]
    skill: NotRequired[dict[str, str]]
    createdAt: str
    completedAt: NotRequired[str]


class TurnFailure(TypedDict):
    code: str
    message: str
    details: NotRequired[dict[str, Any]]


class ClarificationItem(TypedDict):
    prompt: str
    selectionMode: Literal["single", "multiple"]
    answerChoices: list[str]
    response: NotRequired[dict[str, Any]]


class TurnRecord(TypedDict):
    """A saved Turn: a Full Agent Turn, a Realtime Turn, or a Template switch's Handoff Turn.

    ``kind`` is ``"template_handoff"`` for a Handoff Turn, which has no
    ``userMessage`` and an empty ``userMessages``; ``executor`` is
    ``"realtime_model"`` for a Realtime Turn, which also has ``realtime``.
    """

    id: str
    sessionId: str
    status: TurnStatus
    billingTag: BillingTag | None
    agentTemplateId: str | None
    kind: NotRequired[Literal["template_handoff"]]
    executor: NotRequired[Literal["full_agent", "realtime_model"]]
    userMessage: NotRequired[MessageRecord]
    userMessages: NotRequired[list[MessageRecord]]
    userFiles: NotRequired[list[TurnUserFile]]
    operations: list[OperationRecord]
    assistantMessage: MessageRecord | None
    clarificationItem: NotRequired[ClarificationItem]
    failure: NotRequired[TurnFailure]
    realtimeResponses: NotRequired[list[MessageRecord]]
    realtime: NotRequired[dict[str, Any]]
    createdAt: str
    completedAt: NotRequired[str]
    sourceFireId: NotRequired[str | None]


class TurnPage(TypedDict):
    turns: list[TurnRecord]
    hasMore: bool
    nextCursor: str | None


class InteractionMessage(TypedDict):
    id: str
    role: Literal["user", "assistant"]
    content: str
    createdAt: str


class InteractionResult(TypedDict):
    status: TurnStatus
    completedAt: str | None


class SessionInteraction(TypedDict):
    inputId: str
    sequence: int
    kind: Literal["message", "cancel", "retry", "client_tool_output"]
    mode: Literal["started", "steered", "cancelled", "accepted", "rejected"]
    turnId: str
    message: InteractionMessage | None
    createdAt: str
    appliedAt: str | None
    rejection: dict[str, str] | None
    result: InteractionResult


class SessionInteractionPage(TypedDict):
    interactions: list[SessionInteraction]
    hasMore: bool
    nextCursor: str | None


class SessionRealtimeEvent(TypedDict):
    id: str
    type: Literal["input_transcript", "response"]
    realtimeConnectionId: str
    text: str
    turnId: str | None
    createdAt: str
    responseId: NotRequired[str | None]
    kind: NotRequired[RealtimeResponseKind]
    interrupted: NotRequired[bool]


class SessionTemplateSwitch(TypedDict):
    """A completed switch of the Session to another Template."""

    id: str
    commandId: str
    status: Literal["succeeded"]
    configurationEpoch: int
    agentTemplateId: str
    contentDigest: str
    createdAt: str


class SessionTimelineEntry(TypedDict):
    type: Literal["turn", "realtime", "template_switch"]
    turn: NotRequired[TurnRecord]
    realtime: NotRequired[SessionRealtimeEvent]
    templateSwitch: NotRequired[SessionTemplateSwitch]


class SessionTimelinePage(TypedDict):
    entries: list[SessionTimelineEntry]
    hasMore: bool
    nextCursor: str | None


@dataclass(frozen=True)
class TurnState:
    """A Turn and whether anyone asked it to stop."""

    turn: TurnRecord
    #: When a request to stop the Turn's current execution was durably accepted,
    #: or None. The Turn can still be running while the stop converges, and a
    #: checkpoint retry of the Turn starts a new execution without one.
    stop_requested_at: str | None


def session_path(session_id: str) -> str:
    return f"/sessions/{segment(session_id)}"


_MISSING = object()


def _nonempty(value: object) -> bool:
    return isinstance(value, str) and len(value) > 0


def _nullable_text(value: object) -> bool:
    return value is None or isinstance(value, str)


def _safe_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and abs(value) <= 2**53 - 1


_KINDS = ("message", "cancel", "retry", "client_tool_output")
_MODES = ("started", "steered", "cancelled", "accepted", "rejected")
_STATUSES = ("running", "succeeded", "failed", "interrupted")
_REJECTIONS = (
    "interaction_admission_closed",
    "interaction_target_conflict",
    "client_tool_call_unavailable",
    "client_tool_call_settled",
)


def require_interaction(value: object, input_id: str, request_id: str | None = None) -> SessionInteraction:
    if not (
        isinstance(value, dict)
        and value.get("inputId") == input_id
        and _safe_int(value.get("sequence"))
        and value["sequence"] >= 1
        and value.get("kind") in _KINDS
        and value.get("mode") in _MODES
        and _nonempty(value.get("turnId"))
        and isinstance(value.get("createdAt"), str)
        and "appliedAt" in value
        and _nullable_text(value["appliedAt"])
        and isinstance(value.get("result"), dict)
        and value["result"].get("status") in _STATUSES
        and "completedAt" in value["result"]
        and _nullable_text(value["result"]["completedAt"])
        and "message" in value
        and (
            value["message"] is None
            or (
                isinstance(value["message"], dict)
                and _nonempty(value["message"].get("id"))
                and value["message"].get("role") in ("user", "assistant")
                and isinstance(value["message"].get("content"), str)
                and isinstance(value["message"].get("createdAt"), str)
            )
        )
        and "rejection" in value
        and (
            value["rejection"] is None
            or (isinstance(value["rejection"], dict) and value["rejection"].get("code") in _REJECTIONS)
        )
    ):
        raise TiDBLinkError("TiDB Link returned an invalid interaction receipt", request_id=request_id)
    return value  # type: ignore[return-value]


async def list_interactions(
    http: HttpClient,
    session_id: str,
    *,
    limit: int | None = None,
    cursor: str | None = None,
    request_id: str | None = None,
) -> SessionInteractionPage:
    return await http.get(
        f"{session_path(session_id)}/interactions{pagination_query(limit, cursor)}",
        request_id=request_id,
    )


async def get_interaction(
    http: HttpClient, session_id: str, input_id: str, *, request_id: str | None = None
) -> SessionInteraction:
    result = await http.get_response(
        f"{session_path(session_id)}/interactions/{segment(input_id)}", request_id=request_id
    )
    data = result.data if isinstance(result.data, dict) else {}
    return require_interaction(data.get("interaction"), input_id, result.request_id)


async def reconcile_interaction(
    http: HttpClient, session_id: str, client_event_id: str, *, request_id: str | None = None
) -> SessionInteraction | None:
    result = await http.get_response(
        f"{session_path(session_id)}/interactions{query_string({'clientEventId': client_event_id})}",
        request_id=request_id,
    )
    values = result.data.get("interactions") if isinstance(result.data, dict) else None
    if not isinstance(values, list) or len(values) > 1:
        raise TiDBLinkError(
            "TiDB Link returned an invalid interaction lookup", request_id=result.request_id
        )
    return require_interaction(values[0], client_event_id, result.request_id) if values else None


async def history(
    http: HttpClient,
    session_id: str,
    *,
    limit: int | None = None,
    cursor: str | None = None,
    request_id: str | None = None,
) -> TurnPage:
    return await http.get(
        f"{session_path(session_id)}/turns{pagination_query(limit, cursor)}", request_id=request_id
    )


async def _read_turn(
    http: HttpClient, session_id: str, turn_id: str, request_id: str | None
) -> tuple[TurnRecord, Any, str]:
    result = await http.get_response(
        f"{session_path(session_id)}/turns/{segment(turn_id)}", request_id=request_id
    )
    data = result.data if isinstance(result.data, dict) else {}
    turn = data.get("turn")
    if not isinstance(turn, dict) or turn.get("id") != turn_id or turn.get("sessionId") != session_id:
        raise TiDBLinkError("TiDB Link returned a different Turn", request_id=result.request_id)
    return turn, data.get("stopRequestedAt", _MISSING), result.request_id  # type: ignore[return-value]


async def get_turn(
    http: HttpClient, session_id: str, turn_id: str, *, request_id: str | None = None
) -> TurnRecord:
    return (await _read_turn(http, session_id, turn_id, request_id))[0]


def _is_date_time(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


async def get_turn_state(
    http: HttpClient, session_id: str, turn_id: str, *, request_id: str | None = None
) -> TurnState:
    turn, stop_requested_at, response_request_id = await _read_turn(http, session_id, turn_id, request_id)
    if stop_requested_at is not None and not _is_date_time(stop_requested_at):
        raise TiDBLinkError(
            "TiDB Link returned an invalid Turn stop state", request_id=response_request_id
        )
    return TurnState(turn=turn, stop_requested_at=stop_requested_at)


async def timeline(
    http: HttpClient,
    session_id: str,
    *,
    limit: int | None = None,
    cursor: str | None = None,
    request_id: str | None = None,
) -> SessionTimelinePage:
    return await http.get(
        f"{session_path(session_id)}/timeline{pagination_query(limit, cursor)}", request_id=request_id
    )
