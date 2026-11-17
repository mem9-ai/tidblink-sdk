"""Text messages and their original results (``turn.ts`` in the TypeScript SDK)."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from ._http import HttpClient, require_creation_key, segment
from ._types import OMIT, Omittable
from .errors import (
    InputNotAppliedError,
    OutcomeUnknownError,
    TiDBLinkApiError,
    TiDBLinkError,
    TurnFailedError,
    TurnInterruptedError,
    TurnWaitTimeoutError,
)
from .history import (
    BillingTag,
    ClarificationItem,
    SessionInteraction,
    get_interaction,
    get_turn,
    require_interaction,
)

#: Local deadline of a wait, in seconds; it never cancels execution.
DEFAULT_WAIT_TIMEOUT = 60.0
DEFAULT_POLL_INTERVAL = 0.25
_MAX_SECONDS = 2_147_483.647


@dataclass(frozen=True)
class TurnResult:
    """The saved successful answer of an original message or checkpoint retry."""

    #: ``"clarification"`` when the answer asks the user to choose; see ``clarification``.
    type: Literal["answer", "clarification"]
    session_id: str
    input_id: str
    turn_id: str
    message_id: str
    text: str
    interaction: SessionInteraction
    clarification: ClarificationItem | None = None


def valid_clarification(value: object) -> bool:
    if not isinstance(value, dict):
        return False

    def strings(items: object, low: int, high: int) -> bool:
        return (
            isinstance(items, list)
            and low <= len(items) <= high
            and all(isinstance(text, str) and text for text in items)
        )

    mode = value.get("selectionMode")
    if (
        not isinstance(value.get("prompt"), str)
        or not value["prompt"]
        or mode not in ("single", "multiple")
        or not strings(value.get("answerChoices"), 2, 3 if mode == "single" else 8)
    ):
        return False
    if "response" not in value:
        return True
    response = value["response"]
    if not isinstance(response, dict):
        return False
    return (
        sorted(response) == ["answers", "responseTurnId"]
        and isinstance(response["responseTurnId"], str)
        and len(response["responseTurnId"]) > 0
        and strings(response["answers"], 1, 1 if mode == "single" else 9)
    )


def message_data(
    text: str | Sequence[str],
    *,
    user_file_ids: Omittable[Sequence[str]] = OMIT,
    clarification_source_turn_id: Omittable[str] = OMIT,
    billing_tag: Omittable[BillingTag | None] = OMIT,
) -> dict[str, Any]:
    """Check message data before dispatch and return it under its wire names.

    The REST message command and ``session.send`` share it.
    """
    texts = [text] if isinstance(text, str) else text
    if (
        isinstance(texts, (str, bytes))
        or not isinstance(texts, Sequence)
        or not 1 <= len(texts) <= 9
        or any(not isinstance(t, str) or not t for t in texts)
    ):
        raise ValueError("text must contain 1-9 non-empty values")
    data: dict[str, Any] = {"text": text if isinstance(text, str) else list(texts)}
    if user_file_ids is not OMIT:
        if (
            isinstance(user_file_ids, (str, bytes))
            or not isinstance(user_file_ids, Sequence)
            or len(user_file_ids) > 20
            or any(not isinstance(i, str) or not i or len(i) > 64 for i in user_file_ids)
        ):
            raise ValueError("user_file_ids must contain at most 20 non-empty IDs")
        data["userFileIds"] = list(user_file_ids)
    if clarification_source_turn_id is not OMIT:
        if (
            not isinstance(clarification_source_turn_id, str)
            or not clarification_source_turn_id
            or len(clarification_source_turn_id) > 64
        ):
            raise ValueError("clarification_source_turn_id must contain 1-64 characters")
        data["clarificationSourceTurnId"] = clarification_source_turn_id
    if billing_tag is not OMIT:
        data["billingTag"] = billing_tag
    return data


async def submit_message(
    http: HttpClient,
    session_id: str,
    *,
    client_event_id: str,
    data: dict[str, Any],
    request_id: str | None = None,
) -> SessionInteraction:
    require_creation_key(client_event_id, "A message", "client_event_id")
    try:
        result = await http.post_response(
            f"/sessions/{segment(session_id)}/events",
            {"type": "agent.session.input.message", "clientEventId": client_event_id, "data": data},
            request_id=request_id,
            outcome_unknown=True,
        )
        try:
            body = result.data if isinstance(result.data, dict) else {}
            receipt = require_interaction(body.get("interaction"), client_event_id, result.request_id)
            if (
                receipt["kind"] != "message"
                or not receipt["message"]
                or receipt["message"]["role"] != "user"
            ):
                raise TiDBLinkError("Message submission returned a different command receipt")
            return receipt
        except TiDBLinkError as cause:
            raise OutcomeUnknownError(
                "Message submission returned no usable receipt; query the original input",
                request_id=result.request_id,
                cause=cause,
            ) from cause
    except OutcomeUnknownError as cause:
        raise OutcomeUnknownError(
            str(cause),
            session_id=session_id,
            input_id=client_event_id,
            request_id=cause.request_id,
            cause=cause,
        ) from cause


def validate_wait_options(timeout: float | None, poll_interval: float | None) -> tuple[float, float]:
    timeout = DEFAULT_WAIT_TIMEOUT if timeout is None else timeout
    poll_interval = DEFAULT_POLL_INTERVAL if poll_interval is None else poll_interval
    for name, value, low in (("timeout", timeout, 0.0), ("poll_interval", poll_interval, 0.001)):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"{name} must be a number of seconds")
        if not low <= value <= _MAX_SECONDS:
            raise ValueError(f"{name} must be between {low} and {_MAX_SECONDS} seconds")
    return float(timeout), float(poll_interval)


async def wait_for_result(
    http: HttpClient,
    session_id: str,
    input_id: str,
    *,
    timeout: float | None = None,
    poll_interval: float | None = None,
    request_id: str | None = None,
) -> TurnResult:
    timeout, poll_interval = validate_wait_options(timeout, poll_interval)
    require_creation_key(input_id, "Waiting for a result", "input_id")
    seen: dict[str, str] = {}
    deadline = asyncio.timeout(timeout)
    try:
        async with deadline:
            return await _poll_result(http, session_id, input_id, poll_interval, request_id, seen)
    except (TurnFailedError, TurnInterruptedError, InputNotAppliedError, TiDBLinkApiError):
        raise
    except Exception as cause:
        timed_out = isinstance(cause, TimeoutError) and deadline.expired()
        error_class = TurnWaitTimeoutError if timed_out else OutcomeUnknownError
        raise error_class(
            "Local wait timed out; query or wait for the original input"
            if timed_out
            else "Local wait stopped before the original result was established",
            session_id=session_id,
            input_id=input_id,
            turn_id=seen.get("turn_id"),
            request_id=(cause.request_id if isinstance(cause, TiDBLinkError) else None) or request_id,
            cause=cause,
        ) from cause


async def _poll_result(
    http: HttpClient,
    session_id: str,
    input_id: str,
    poll_interval: float,
    request_id: str | None,
    seen: dict[str, str],
) -> TurnResult:
    while True:
        interaction = await get_interaction(http, session_id, input_id, request_id=request_id)
        turn_id = interaction["turnId"]
        seen["turn_id"] = turn_id
        kind = interaction["kind"]
        message = interaction["message"]
        if (
            kind not in ("message", "retry")
            or (kind == "message" and (not message or message["role"] != "user"))
            or (kind == "retry" and message is not None)
        ):
            raise TiDBLinkError("Waiting for a result requires a message or checkpoint retry receipt")
        if interaction["rejection"] or interaction["mode"] == "rejected":
            raise InputNotAppliedError(session_id, input_id, turn_id)
        status = interaction["result"]["status"]
        completed_at = interaction["result"]["completedAt"]
        if status != "running" and not completed_at:
            raise TiDBLinkError("Original terminal receipt omitted its completion time")
        if status == "failed":
            raise TurnFailedError(
                code="turn_failed",
                message="The original Turn execution failed",
                turn_id=turn_id,
                session_id=session_id,
                input_id=input_id,
            )
        if status == "interrupted":
            raise TurnInterruptedError(turn_id, session_id=session_id, input_id=input_id)
        if status == "succeeded":
            if kind == "message" and not interaction["appliedAt"]:
                raise InputNotAppliedError(session_id, input_id, turn_id)
            turn = await get_turn(http, session_id, turn_id, request_id=request_id)
            answer: Any = turn.get("assistantMessage")
            clarification = turn.get("clarificationItem")
            if (
                turn.get("executor") == "realtime_model"
                or turn.get("status") != "succeeded"
                or turn.get("completedAt") != completed_at
                or not isinstance(answer, dict)
                or answer.get("role") != "assistant"
                or not isinstance(answer.get("id"), str)
                or not answer["id"]
                or not isinstance(answer.get("content"), str)
                or answer.get("turnId") != turn_id
                or answer.get("sessionId") != session_id
                or ("clarificationItem" in turn and not valid_clarification(clarification))
            ):
                raise TiDBLinkError("Saved Turn does not match the original successful execution")
            return TurnResult(
                type="clarification" if clarification else "answer",
                session_id=session_id,
                input_id=input_id,
                turn_id=turn_id,
                message_id=answer["id"],
                text=answer["content"],
                interaction=interaction,
                clarification=clarification or None,
            )
        await asyncio.sleep(poll_interval)
