"""Prerecorded audio Turns (``audio.ts`` in the TypeScript SDK).

An audio Turn's request owns its response: the events arrive on it, and
losing or closing it may request remote interruption of the Turn.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass
from typing import Any, Literal, NotRequired, TypedDict, Union

import httpx

from ._http import (
    utf16_length,
    _BODY_CHUNK,
    _REQUEST_ID,
    Bytes,
    BytesBody,
    HttpClient,
    _dump_json,
    byte_view,
    media_type,
    require_hashed_length,
    segment,
)
from ._sse import decode_sse
from ._types import OMIT, Omittable
from .errors import (
    OutcomeUnknownError,
    TiDBLinkApiError,
    TiDBLinkError,
    TurnFailedError,
    TurnInterruptedError,
)
from .events import ClientToolCallsPayload, NotifiedFile, is_client_tool_calls_payload
from .history import BillingTag, ClarificationItem, OperationStatus, TurnFailure
from .turn import valid_clarification

__all__ = [
    "AudioTurnEvent",
    "AudioTurnResult",
    "AudioTurnStoppedError",
    "audio_turn",
    "stream_audio",
]

_MAX_AUDIO_BYTES = 25 * 1024 * 1024
_MEDIA_TYPE = re.compile(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+")
_EVENTS = frozenset(
    {
        "turn_started",
        "progress",
        "assistant_draft",
        "operation_step",
        "assistant_message",
        "client_tool_calls",
        "turn_error",
        "turn_finished",
    }
)
_TERMINAL = ("succeeded", "failed", "interrupted")
_MAX_SAFE_INTEGER = 2**53 - 1


# --- Events ------------------------------------------------------------------


class AudioTurnExecution(TypedDict):
    model: str


class AudioTurnStartedPayload(TypedDict):
    execution: NotRequired[AudioTurnExecution]
    inputId: NotRequired[str]


class AudioTextPayload(TypedDict):
    text: str


class AudioOperationStepPayload(TypedDict):
    operationId: str
    status: OperationStatus
    label: str
    category: NotRequired[Literal["tool_call", "skill_read"]]
    skill: NotRequired[dict[str, str]]
    source: NotRequired[dict[str, Any]]
    #: On the succeeded step of a File Notification: the file it announced.
    file: NotRequired[NotifiedFile]


class AudioAssistantMessagePayload(TypedDict):
    messageId: str
    text: str
    clarificationItem: NotRequired[ClarificationItem]


class AudioTurnFinishedPayload(TypedDict):
    status: Literal["succeeded", "failed", "interrupted"]


class _AudioEnvelope(TypedDict):
    sessionId: str
    turnId: str
    agentRunId: NotRequired[str]
    seq: int
    createdAt: str


class AudioTurnStartedEvent(_AudioEnvelope):
    event: Literal["turn_started"]
    payload: AudioTurnStartedPayload


class AudioProgressEvent(_AudioEnvelope):
    event: Literal["progress"]
    payload: AudioTextPayload


class AudioAssistantDraftEvent(_AudioEnvelope):
    event: Literal["assistant_draft"]
    payload: AudioTextPayload


class AudioOperationStepEvent(_AudioEnvelope):
    event: Literal["operation_step"]
    payload: AudioOperationStepPayload


class AudioAssistantMessageEvent(_AudioEnvelope):
    event: Literal["assistant_message"]
    payload: AudioAssistantMessagePayload


class AudioClientToolCallsEvent(_AudioEnvelope):
    """Client Tool calls the audio Turn published.

    The Session's executing connection claims, runs and answers them; this
    event authorizes nothing. A call made while no connection executes the
    Session is not performed and gets no frame.
    """

    event: Literal["client_tool_calls"]
    payload: ClientToolCallsPayload


class AudioTurnErrorEvent(_AudioEnvelope):
    event: Literal["turn_error"]
    payload: TurnFailure


class AudioTurnFinishedEvent(_AudioEnvelope):
    event: Literal["turn_finished"]
    payload: AudioTurnFinishedPayload


AudioTurnEvent = Union[
    AudioTurnStartedEvent,
    AudioProgressEvent,
    AudioAssistantDraftEvent,
    AudioOperationStepEvent,
    AudioAssistantMessageEvent,
    AudioClientToolCallsEvent,
    AudioTurnErrorEvent,
    AudioTurnFinishedEvent,
]


@dataclass(frozen=True)
class AudioTurnResult:
    """The final answer of an audio Turn that succeeded."""

    #: ``"clarification"`` when the answer asks the user to choose; see ``clarification``.
    type: Literal["answer", "clarification"]
    session_id: str
    turn_id: str
    message_id: str
    text: str
    clarification: ClarificationItem | None = None


class AudioTurnStoppedError(OutcomeUnknownError):
    """Audio owns its response: losing or closing it may request remote interruption."""

    def __init__(
        self,
        session_id: str,
        turn_id: str | None = None,
        request_id: str | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(
            "Audio response stopped before a confirmed terminal outcome; closing it may "
            "request interruption. Inspect saved history before submitting audio again.",
            session_id=session_id,
            turn_id=turn_id or None,
            request_id=request_id or None,
            cause=cause,
        )


# --- Request body ------------------------------------------------------------


class _MultipartBody(BytesBody):
    """A multipart body that sends the recording's bytes in place, between its framing."""

    def __init__(self, head: bytes, view: memoryview, tail: bytes) -> None:
        super().__init__(view, view.nbytes)
        self._head = head
        self._tail = tail
        self.content_length = len(head) + view.nbytes + len(tail)

    async def __aiter__(self) -> AsyncIterator[bytes]:
        if self.pulled:
            raise RuntimeError("A byte body can be sent only once")
        try:
            require_hashed_length(self._view, self._length)
        except ValueError as error:
            self.error = error
            raise
        self.pulled = True
        yield self._head
        for start in range(0, self._length, _BODY_CHUNK):
            yield self._view[start : start + _BODY_CHUNK]  # type: ignore[misc]
        yield self._tail


def _multipart(
    data: object,
    filename: object,
    content_type: object,
    billing_tag: Omittable[BillingTag | None],
    request_id: object,
) -> tuple[_MultipartBody, str]:
    view = byte_view(data, "Audio data")
    if not 1 <= view.nbytes <= _MAX_AUDIO_BYTES:
        raise ValueError("Audio requires 1 byte to 25 MiB of prerecorded data")
    if not isinstance(filename, str):
        raise TypeError("Audio requires a filename of 1-255 characters")
    if not filename.strip() or utf16_length(filename) > 255:
        raise ValueError("Audio requires a filename of 1-255 characters")
    try:
        encoded_filename = (
            filename.replace("\r", "%0D").replace("\n", "%0A").replace('"', "%22").encode()
        )
    except UnicodeEncodeError:
        raise ValueError("Audio requires a filename of 1-255 characters") from None
    media_type = "application/octet-stream" if content_type is None else content_type
    if not isinstance(media_type, str):
        raise TypeError("Audio content_type must be a media type without parameters")
    if not _MEDIA_TYPE.fullmatch(media_type):
        raise ValueError("Audio content_type must be a media type without parameters")
    if request_id is not None and (
        not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id)
    ):
        raise ValueError("request_id must be 1-64 safe characters")
    boundary = f"tidb-link-audio-{uuid.uuid4()}"
    head = b""
    if billing_tag is not OMIT:
        head += (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"billingTag\"\r\n"
            "Content-Type: application/json\r\n\r\n"
        ).encode() + _dump_json(billing_tag) + b"\r\n"
    head += (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"".encode()
        + encoded_filename
        + f"\"\r\nContent-Type: {media_type}\r\n\r\n".encode()
    )
    tail = f"\r\n--{boundary}--\r\n".encode()
    return _MultipartBody(head, view, tail), f"multipart/form-data; boundary={boundary}"


# --- Events on the response --------------------------------------------------


def _valid_envelope(
    event: object, frame_event: str, session_id: str, turn_id: str | None, sequence: int
) -> bool:
    if not isinstance(event, dict):
        return False
    seq = event.get("seq")
    event_turn_id = event.get("turnId")
    return (
        event.get("sessionId") == session_id
        and isinstance(event_turn_id, str)
        and bool(event_turn_id)
        and (not turn_id or event_turn_id == turn_id)
        and isinstance(seq, int)
        and not isinstance(seq, bool)
        and abs(seq) <= _MAX_SAFE_INTEGER
        and seq > sequence
        and isinstance(event.get("createdAt"), str)
        and isinstance(event.get("payload"), dict)
        and frame_event == event.get("event")
        and event.get("event") in _EVENTS
    )


def _check_payload(name: str, payload: dict[str, Any]) -> None:
    if name in ("assistant_draft", "progress") and not isinstance(payload.get("text"), str):
        raise TiDBLinkError("Audio response returned invalid text")
    if name == "assistant_message" and (
        not isinstance(payload.get("messageId"), str)
        or not payload["messageId"]
        or not isinstance(payload.get("text"), str)
        or ("clarificationItem" in payload and not valid_clarification(payload["clarificationItem"]))
    ):
        raise TiDBLinkError("Audio response returned an invalid final Message")
    if name == "client_tool_calls" and not is_client_tool_calls_payload(payload):
        raise TiDBLinkError("Audio response returned invalid Client Tool calls")
    if name == "turn_finished" and payload.get("status") not in _TERMINAL:
        raise TiDBLinkError("Audio response returned an invalid terminal status")


async def stream_audio(
    http: HttpClient,
    session_id: str,
    data: Bytes,
    *,
    filename: str,
    content_type: str | None = None,
    billing_tag: Omittable[BillingTag | None] = OMIT,
    request_id: str | None = None,
) -> AsyncIterator[AudioTurnEvent]:
    """Submit a recording and yield the audio Turn's events from the request's own response.

    ``billing_tag`` is omitted to inherit, or None to clear the Session's
    default for this Turn. The stream ends after ``turn_finished``. Closing it
    early (``aclose()``, or leaving ``async with contextlib.aclosing(...)``)
    closes the response, which may request interruption of the Turn.
    """
    body, multipart_type = _multipart(data, filename, content_type, billing_tag, request_id)
    turn_id: str | None = None
    stable_request_id: str | None = None
    sequence = -1
    response: httpx.Response | None = None
    try:
        response, stable_request_id = await http.raw_response(
            f"/sessions/{segment(session_id)}/turns/audio",
            method="POST",
            content=body,
            headers={
                "accept": "text/event-stream",
                "content-type": multipart_type,
                "content-length": str(body.content_length),
            },
            request_id=request_id,
        )
        if response.status_code != 201 or media_type(response) != "text/event-stream":
            raise TiDBLinkError("Audio response omitted its event stream")
        async for frame in decode_sse(response.aiter_bytes()):
            event = json.loads(frame.data)
            if not _valid_envelope(event, frame.event, session_id, turn_id, sequence):
                raise TiDBLinkError(
                    "Audio response returned an invalid or mismatched event envelope"
                )
            turn_id = event["turnId"]
            sequence = event["seq"]
            _check_payload(event["event"], event["payload"])
            yield event
            if event["event"] == "turn_finished":
                return
        raise AudioTurnStoppedError(session_id, turn_id, stable_request_id)
    except (TiDBLinkApiError, AudioTurnStoppedError):
        raise
    except Exception as cause:
        raise AudioTurnStoppedError(
            session_id,
            turn_id,
            stable_request_id
            or (cause.request_id if isinstance(cause, TiDBLinkError) else request_id),
            cause,
        ) from cause
    finally:
        if response is not None:
            await response.aclose()


async def audio_turn(
    http: HttpClient,
    session_id: str,
    data: Bytes,
    *,
    filename: str,
    content_type: str | None = None,
    billing_tag: Omittable[BillingTag | None] = OMIT,
    request_id: str | None = None,
) -> AudioTurnResult:
    """Submit a recording and wait on its own response for the audio Turn's final answer."""
    message: AudioAssistantMessagePayload | None = None
    failure: TurnFailure | None = None
    events = stream_audio(
        http,
        session_id,
        data,
        filename=filename,
        content_type=content_type,
        billing_tag=billing_tag,
        request_id=request_id,
    )
    async with aclosing(events):
        async for event in events:
            if event["event"] == "assistant_message":
                message = event["payload"]
            if event["event"] == "turn_error":
                failure = event["payload"]
            if event["event"] == "turn_finished":
                status = event["payload"]["status"]
                if status == "failed":
                    code = failure.get("code") if failure else None
                    text = failure.get("message") if failure else None
                    raise TurnFailedError(
                        session_id=session_id,
                        turn_id=event["turnId"],
                        code="turn_failed" if code is None else code,
                        message="Audio Turn failed" if text is None else text,
                    )
                if status == "interrupted":
                    raise TurnInterruptedError(
                        event["turnId"], "Audio Turn was interrupted", session_id
                    )
                if message is None:
                    raise AudioTurnStoppedError(session_id, event["turnId"])
                clarification = message.get("clarificationItem")
                return AudioTurnResult(
                    type="clarification" if clarification else "answer",
                    session_id=session_id,
                    turn_id=event["turnId"],
                    message_id=message["messageId"],
                    text=message["text"],
                    clarification=clarification or None,
                )
    raise AudioTurnStoppedError(session_id)
