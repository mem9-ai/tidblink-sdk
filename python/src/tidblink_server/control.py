"""Cancellation and checkpoint retry of an exact Turn (``control.ts`` in the TypeScript SDK)."""

from __future__ import annotations

from typing import Literal, TypedDict

from ._http import HttpClient, require_creation_key, segment
from .errors import OutcomeUnknownError
from .history import SessionInteraction, require_interaction


class RetryReceipt(TypedDict):
    clientEventId: str
    sessionId: str
    turnId: str
    disposition: Literal["retrying"]


class _UnusableReceipt(Exception):
    pass


async def submit_turn_command(
    http: HttpClient,
    session_id: str,
    kind: Literal["cancel", "retry"],
    *,
    client_event_id: str,
    turn_id: str,
    request_id: str | None = None,
) -> SessionInteraction | RetryReceipt:
    require_creation_key(client_event_id, "Turn command", "client_event_id")
    if not isinstance(turn_id, str) or not turn_id or len(turn_id) > 64:
        raise (ValueError if isinstance(turn_id, str) else TypeError)("Turn command requires a turn_id containing 1-64 characters")
    try:
        result = await http.post_response(
            f"/sessions/{segment(session_id)}/events",
            {
                "type": f"agent.session.input.{kind}",
                "clientEventId": client_event_id,
                "data": {"turnId": turn_id},
            },
            outcome_unknown=True,
            request_id=request_id,
        )
        body = result.data if isinstance(result.data, dict) else {}
        try:
            if kind == "cancel":
                receipt = require_interaction(body.get("interaction"), client_event_id, result.request_id)
                if receipt["kind"] != kind or receipt["turnId"] != turn_id or receipt["message"] is not None:
                    raise _UnusableReceipt("Cancellation receipt identifies a different command")
                return receipt
            event = body.get("event")
            if (
                not isinstance(event, dict)
                or event.get("clientEventId") != client_event_id
                or event.get("sessionId") != session_id
                or event.get("turnId") != turn_id
                or event.get("disposition") != "retrying"
            ):
                raise _UnusableReceipt("Retry receipt identifies a different command")
            return event  # type: ignore[return-value]
        except Exception as cause:
            raise OutcomeUnknownError(
                "Turn command returned no usable receipt; query its original identity",
                request_id=result.request_id,
                cause=cause,
            ) from cause
    except OutcomeUnknownError as cause:
        raise OutcomeUnknownError(
            str(cause),
            session_id=session_id,
            input_id=client_event_id,
            turn_id=turn_id,
            request_id=cause.request_id,
            cause=cause,
        ) from cause
