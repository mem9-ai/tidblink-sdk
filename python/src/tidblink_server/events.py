"""Session events delivered over a Session connection (``events.ts`` in the TypeScript SDK).

Events keep the service's field names. Events whose ``type`` the SDK does not
know are delivered unchanged; use :func:`as_known_session_event` to narrow one
to a known form. Display events remain provisional; use the original
interaction to establish a result.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Generic, Literal, NotRequired, TypedDict, TypeVar, Union

from .errors import TiDBLinkError
from .history import ClarificationItem, RealtimeResponseKind

__all__ = [
    "SessionEvent",
    "KnownSessionEvent",
    "TurnEventData",
    "TurnStartedEvent",
    "TurnDraftEvent",
    "TurnProgressEvent",
    "TurnItemAddedEvent",
    "OperationStepPayload",
    "TurnRequiresActionEvent",
    "ClientToolCallPayload",
    "TurnCompletedEvent",
    "TurnCancelledEvent",
    "TurnFailedEvent",
    "RealtimeTranscript",
    "RealtimeResponse",
    "RealtimeTranscriptEvent",
    "RealtimeResponseEvent",
    "TemplateSwitchEvent",
    "TemplateSwitchEventData",
    "SessionIdleData",
    "SessionIdleEvent",
    "as_known_session_event",
    "session_event_from",
    "is_client_tool_calls_payload",
]


class SessionEvent(TypedDict):
    """One event of a Session's event stream."""

    #: Opaque stream position. Persist it to resume after this event.
    cursor: str
    type: str
    sessionId: str
    turnId: str | None
    createdAt: str
    data: dict[str, Any]


P = TypeVar("P")


class TurnEventData(TypedDict, Generic[P]):
    """The durable Turn event carried in ``data`` of every ``agent.session.turn.*`` event."""

    event: str
    turnId: str
    sessionId: str
    agentRunId: NotRequired[str]
    seq: int
    createdAt: str
    payload: P


class TurnExecution(TypedDict):
    model: str


class TurnStartedPayload(TypedDict, total=False):
    inputId: str
    execution: TurnExecution


class TextPayload(TypedDict):
    text: str


class AssistantMessagePayload(TypedDict):
    messageId: str
    text: str
    clarificationItem: NotRequired[ClarificationItem]


class OperationStepSkill(TypedDict):
    skillId: str
    name: str


class OperationStepPayload(TypedDict):
    operationId: str
    status: Literal["running", "succeeded", "failed", "interrupted", "unknown_result"]
    label: str
    category: NotRequired[Literal["tool_call", "skill_read"]]
    skill: NotRequired[OperationStepSkill]
    #: ``{"type": "session_client"}``, or another source with more fields.
    source: NotRequired[dict[str, Any]]


class ClientToolCallPayload(TypedDict):
    """A Client Tool call carried by ``agent.session.turn.requires_action``."""

    callId: str
    name: str
    arguments: dict[str, Any]
    deadlineAt: str
    #: The end of the call's claim window. Only the Session's executing Backend
    #: Connection attachment executes the call, after claiming it before this
    #: time; this event authorizes nothing. Absent only on calls recorded before
    #: service c21a5e3a, which removed the per-Session subscription.
    claimBy: NotRequired[str]


class ClientToolCallsPayload(TypedDict):
    calls: list[ClientToolCallPayload]


class TurnFinishedPayload(TypedDict):
    status: Literal["succeeded", "interrupted", "failed"]


class TurnErrorPayload(TypedDict):
    code: str
    message: str
    details: NotRequired[dict[str, Any]]


class TurnStartedEvent(TypedDict):
    cursor: str
    type: Literal["agent.session.turn.started"]
    sessionId: str
    turnId: str
    createdAt: str
    data: TurnEventData[TurnStartedPayload]


class TurnDraftEvent(TypedDict):
    """The complete provisional draft; replace the previous draft of this Turn."""

    cursor: str
    type: Literal["agent.session.turn.output_text.snapshot"]
    sessionId: str
    turnId: str
    createdAt: str
    data: TurnEventData[TextPayload]


class TurnProgressEvent(TypedDict):
    cursor: str
    type: Literal["agent.session.turn.progress"]
    sessionId: str
    turnId: str
    createdAt: str
    data: TurnEventData[TextPayload]


class TurnItemAddedEvent(TypedDict):
    """``data.event`` is ``assistant_message`` or ``operation_step``."""

    cursor: str
    type: Literal["agent.session.turn.item.added"]
    sessionId: str
    turnId: str
    createdAt: str
    data: TurnEventData[AssistantMessagePayload] | TurnEventData[OperationStepPayload]


class TurnRequiresActionEvent(TypedDict):
    cursor: str
    type: Literal["agent.session.turn.requires_action"]
    sessionId: str
    turnId: str
    createdAt: str
    data: TurnEventData[ClientToolCallsPayload]


class TurnCompletedEvent(TypedDict):
    cursor: str
    type: Literal["agent.session.turn.completed"]
    sessionId: str
    turnId: str
    createdAt: str
    data: TurnEventData[TurnFinishedPayload]


class TurnCancelledEvent(TypedDict):
    cursor: str
    type: Literal["agent.session.turn.cancelled"]
    sessionId: str
    turnId: str
    createdAt: str
    data: TurnEventData[TurnFinishedPayload]


class TurnFailedEvent(TypedDict):
    """``data.event`` is ``turn_finished`` or ``turn_error``."""

    cursor: str
    type: Literal["agent.session.turn.failed"]
    sessionId: str
    turnId: str
    createdAt: str
    data: TurnEventData[TurnFinishedPayload] | TurnEventData[TurnErrorPayload]


class RealtimeTranscript(TypedDict):
    """A completed Realtime user transcript."""

    id: str
    type: Literal["input_transcript"]
    realtimeConnectionId: str
    text: str
    turnId: str | None
    createdAt: str


class RealtimeResponse(TypedDict):
    """A completed Realtime model response."""

    id: str
    type: Literal["response"]
    realtimeConnectionId: str
    text: str
    turnId: str | None
    createdAt: str
    responseId: NotRequired[str | None]
    kind: RealtimeResponseKind
    interrupted: NotRequired[bool]


class RealtimeTranscriptEvent(TypedDict):
    cursor: str
    type: Literal["agent.session.realtime.input_transcript.completed"]
    sessionId: str
    turnId: str | None
    createdAt: str
    data: RealtimeTranscript


class RealtimeResponseEvent(TypedDict):
    cursor: str
    type: Literal["agent.session.realtime.response.completed"]
    sessionId: str
    turnId: str | None
    createdAt: str
    data: RealtimeResponse


class TemplateSwitchEventData(TypedDict):
    id: str
    commandId: str
    status: Literal["succeeded", "failed"]
    configurationEpoch: int
    agentTemplateId: str
    contentDigest: str | None
    createdAt: str


class TemplateSwitchEvent(TypedDict):
    cursor: str
    type: Literal["agent.session.template_switch.succeeded", "agent.session.template_switch.failed"]
    sessionId: str
    turnId: str | None
    createdAt: str
    data: TemplateSwitchEventData


class SessionIdleData(TypedDict):
    id: str
    #: The released Turn, or None when it was never recorded.
    turnId: str | None
    createdAt: str


class SessionIdleEvent(TypedDict):
    """The Session released its active Turn: the next message, audio Turn or Template switch can start.

    It follows that Turn's terminal events and does not establish its
    outcome; it can repeat for one Turn.
    """

    cursor: str
    type: Literal["agent.session.idle"]
    sessionId: str
    turnId: str | None
    createdAt: str
    data: SessionIdleData


KnownSessionEvent = Union[
    TurnStartedEvent,
    TurnDraftEvent,
    TurnProgressEvent,
    TurnItemAddedEvent,
    TurnRequiresActionEvent,
    TurnCompletedEvent,
    TurnCancelledEvent,
    TurnFailedEvent,
    RealtimeTranscriptEvent,
    RealtimeResponseEvent,
    TemplateSwitchEvent,
    SessionIdleEvent,
]


def _is_string(value: object) -> bool:
    return isinstance(value, str)


def _is_object(value: object) -> bool:
    return isinstance(value, dict)


#: Turn events name their durable event in ``data.event``.
_TURN_EVENTS: dict[str, tuple[str, ...]] = {
    "agent.session.turn.started": ("turn_started",),
    "agent.session.turn.output_text.snapshot": ("assistant_draft",),
    "agent.session.turn.progress": ("progress",),
    "agent.session.turn.item.added": ("assistant_message", "operation_step"),
    "agent.session.turn.requires_action": ("client_tool_calls",),
    "agent.session.turn.completed": ("turn_finished",),
    "agent.session.turn.cancelled": ("turn_finished",),
    "agent.session.turn.failed": ("turn_finished", "turn_error"),
}
_FINISHED_STATUS = {
    "agent.session.turn.completed": "succeeded",
    "agent.session.turn.cancelled": "interrupted",
    "agent.session.turn.failed": "failed",
}


def is_client_tool_calls_payload(payload: Mapping[str, Any]) -> bool:
    """Whether a ``client_tool_calls`` payload has the fields the SDK types."""
    calls = payload.get("calls")
    return isinstance(calls, list) and all(
        _is_object(call)
        and _is_string(call.get("callId"))
        and _is_string(call.get("name"))
        and _is_object(call.get("arguments"))
        and _is_string(call.get("deadlineAt"))
        and ("claimBy" not in call or _is_string(call["claimBy"]))
        for call in calls
    )


def _is_known_turn_payload(name: str, payload: Mapping[str, Any], type_: str) -> bool:
    if name in ("progress", "assistant_draft"):
        return _is_string(payload.get("text"))
    if name == "assistant_message":
        return _is_string(payload.get("text")) and _is_string(payload.get("messageId"))
    if name == "operation_step":
        return (
            _is_string(payload.get("operationId"))
            and _is_string(payload.get("status"))
            and _is_string(payload.get("label"))
        )
    if name == "client_tool_calls":
        return is_client_tool_calls_payload(payload)
    if name == "turn_finished":
        return payload.get("status") == _FINISHED_STATUS.get(type_)
    if name == "turn_error":
        return _is_string(payload.get("code")) and _is_string(payload.get("message"))
    return True


def as_known_session_event(event: SessionEvent) -> KnownSessionEvent | None:
    """Narrow an event to one of the types the SDK knows.

    Returns None for any other type, and for a known type whose data lacks
    the fields the SDK types; such events are still delivered unchanged.
    """
    type_ = event["type"]
    data = event["data"]
    turn_events = _TURN_EVENTS.get(type_)
    if turn_events is not None:
        name = data.get("event")
        payload = data.get("payload")
        if (
            not _is_string(event.get("turnId"))
            or not _is_string(name)
            or name not in turn_events
            or not _is_object(payload)
            or not _is_known_turn_payload(name, payload, type_)
        ):
            return None
        return event  # type: ignore[return-value]
    if type_ in (
        "agent.session.realtime.input_transcript.completed",
        "agent.session.realtime.response.completed",
    ):
        expected = (
            "input_transcript"
            if type_ == "agent.session.realtime.input_transcript.completed"
            else "response"
        )
        if (
            data.get("type") == expected
            and _is_string(data.get("id"))
            and _is_string(data.get("text"))
            and _is_string(data.get("realtimeConnectionId"))
        ):
            return event  # type: ignore[return-value]
        return None
    if type_ in ("agent.session.template_switch.succeeded", "agent.session.template_switch.failed"):
        epoch = data.get("configurationEpoch")
        if (
            _is_string(data.get("commandId"))
            and _is_string(data.get("agentTemplateId"))
            and isinstance(epoch, (int, float))
            and not isinstance(epoch, bool)
            and data.get("status") == type_[len("agent.session.template_switch.") :]
        ):
            return event  # type: ignore[return-value]
        return None
    if type_ == "agent.session.idle":
        if _is_string(data.get("id")) and (data.get("turnId") is None or _is_string(data.get("turnId"))):
            return event  # type: ignore[return-value]
        return None
    return None


def session_event_from(
    value: object,
    cursor: object,
    *,
    session_id: str | None = None,
    request_id: str | None = None,
) -> SessionEvent:
    """Check one event envelope and attach its cursor."""
    if (
        not isinstance(cursor, str)
        or not cursor
        or not isinstance(value, dict)
        or not isinstance(value.get("type"), str)
        or not value["type"]
        or not isinstance(value.get("sessionId"), str)
        or (session_id is not None and value["sessionId"] != session_id)
        or "turnId" not in value
        or (value["turnId"] is not None and not isinstance(value["turnId"], str))
        or not isinstance(value.get("createdAt"), str)
        or not isinstance(value.get("data"), dict)
    ):
        raise TiDBLinkError("Session event has an invalid envelope", request_id=request_id)
    return {**value, "cursor": cursor}  # type: ignore[typeddict-item]
