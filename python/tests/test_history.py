"""Ported from typescript/test/history.test.mjs."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from core_support import session_for
from support import json_response
from tidblink_server import TiDBLinkApiError, TiDBLinkError, TurnState

CREATED_AT = "2026-10-28T00:00:00.000Z"
SESSION_PATH = "/api/v1/org/users/user_1/sessions/session%2F1"
TURN: dict[str, Any] = {
    "id": "turn_1",
    "sessionId": "session/1",
    "executor": "full_agent",
    "status": "succeeded",
    "billingTag": None,
    "operations": [],
    "createdAt": CREATED_AT,
    "completedAt": CREATED_AT,
    "userMessage": {
        "id": "msg_1",
        "sessionId": "session/1",
        "turnId": "turn_1",
        "role": "user",
        "content": "Hello",
        "createdAt": CREATED_AT,
        "inputContext": {"channel": "text"},
    },
    "assistantMessage": {
        "id": "msg_2",
        "sessionId": "session/1",
        "turnId": "turn_1",
        "role": "assistant",
        "content": "Hi",
        "createdAt": CREATED_AT,
    },
}
INTERACTION: dict[str, Any] = {
    "inputId": "command/1",
    "sequence": 1,
    "kind": "message",
    "mode": "started",
    "turnId": "turn_1",
    "message": {"id": "msg_1", "role": "user", "content": "Hello", "createdAt": CREATED_AT},
    "createdAt": CREATED_AT,
    "appliedAt": CREATED_AT,
    "rejection": None,
    "result": {"status": "succeeded", "completedAt": CREATED_AT},
}


def history_session(handler):
    return session_for(handler, "session/1")


async def test_history_preserves_turn_pages_and_selected_full_agent_details() -> None:
    page = {"turns": [TURN], "hasMore": True, "nextCursor": "opaque+/="}
    session, service = history_session(
        lambda request, service: json_response({"turn": TURN} if request.url.path.endswith("/turn_1") else page)
    )
    assert await session.history(limit=1, cursor="opaque+/=") == page
    assert await session.get_turn("turn_1") == TURN
    assert [call.path for call in service.calls] == [
        f"{SESSION_PATH}/turns?limit=1&cursor=opaque%2B%2F%3D",
        f"{SESSION_PATH}/turns/turn_1",
    ]


async def test_get_turn_state_returns_the_turn_with_when_a_stop_was_requested() -> None:
    body: dict[str, Any] = {"turn": TURN, "stopRequestedAt": None}
    session, service = history_session(lambda request, service: json_response(body))
    assert await session.get_turn_state("turn_1") == TurnState(turn=TURN, stop_requested_at=None)
    body = {"turn": TURN, "stopRequestedAt": CREATED_AT}
    assert await session.get_turn_state("turn_1") == TurnState(turn=TURN, stop_requested_at=CREATED_AT)
    for stop_requested_at in [OMITTED := object(), 0, "", "soon"]:
        body = {"turn": TURN}
        if stop_requested_at is not OMITTED:
            body["stopRequestedAt"] = stop_requested_at
        with pytest.raises(TiDBLinkError, match="invalid Turn stop state"):
            await session.get_turn_state("turn_1")
    body = {"turn": {**TURN, "id": "turn_2"}, "stopRequestedAt": None}
    with pytest.raises(TiDBLinkError, match="different Turn"):
        await session.get_turn_state("turn_1")
    assert [call.path for call in service.calls] == [f"{SESSION_PATH}/turns/turn_1"] * 7


async def test_interaction_pagination_and_exact_command_reconciliation_preserve_original_receipts() -> None:
    page = {"interactions": [INTERACTION], "hasMore": True, "nextCursor": "interaction-cursor"}

    def handler(request, service):
        path = service.calls[-1].path
        if "clientEventId=missing" in path:
            return json_response({"interactions": [], "hasMore": False, "nextCursor": None})
        if "clientEventId=" in path:
            return json_response({"interactions": [INTERACTION], "hasMore": False, "nextCursor": None})
        if path.endswith("command%2F1"):
            return json_response({"interaction": INTERACTION})
        return json_response(page)

    session, service = history_session(handler)
    assert await session.list_interactions(limit=1, cursor="interaction-cursor") == page
    assert await session.get_interaction("command/1") == INTERACTION
    assert await session.reconcile_interaction("command/1") == INTERACTION
    assert await session.reconcile_interaction("missing") is None
    assert [call.path for call in service.calls] == [
        f"{SESSION_PATH}/interactions?limit=1&cursor=interaction-cursor",
        f"{SESSION_PATH}/interactions/command%2F1",
        f"{SESSION_PATH}/interactions?clientEventId=command%2F1",
        f"{SESSION_PATH}/interactions?clientEventId=missing",
    ]


async def test_mixed_history_and_timeline_preserve_realtime_projections_and_opaque_continuation() -> None:
    audio_message = {
        **TURN["userMessage"],
        "turnId": "turn_rt_1",
        "inputContext": {"channel": "audio"},
        "realtime": {"startsTurn": True, "linkedTurnId": "turn_1"},
    }
    realtime = {
        **TURN,
        "id": "turn_rt_1",
        "executor": "realtime_model",
        "userMessage": audio_message,
        "userMessages": [audio_message],
        "realtime": {"connectionId": "rtc_1", "linkedTurnIds": ["turn_1"]},
        "assistantMessage": {
            **TURN["assistantMessage"],
            "turnId": "turn_rt_1",
            "realtime": {
                "kind": "task_result",
                "interrupted": True,
                "responseId": "response_1",
                "linkedTurnId": "turn_1",
            },
        },
    }
    fact = {
        "id": "rt_1",
        "type": "response",
        "realtimeConnectionId": "rtc_1",
        "text": "Hi",
        "turnId": "turn_1",
        "responseId": "response_1",
        "kind": "task_result",
        "interrupted": True,
        "createdAt": CREATED_AT,
    }
    history_page = {"turns": [TURN, realtime], "hasMore": False, "nextCursor": None}
    timeline_page = {
        "entries": [{"type": "turn", "turn": TURN}, {"type": "realtime", "realtime": fact}],
        "hasMore": True,
        "nextCursor": "timeline+/=",
    }
    session, service = history_session(
        lambda request, service: json_response(
            timeline_page if "/timeline" in request.url.path else history_page
        )
    )
    assert await session.history() == history_page
    assert await session.timeline(limit=2, cursor="timeline+/=") == timeline_page
    assert [call.path for call in service.calls] == [
        f"{SESSION_PATH}/turns",
        f"{SESSION_PATH}/timeline?limit=2&cursor=timeline%2B%2F%3D",
    ]


async def test_receipt_reads_reject_mismatched_or_unusable_identities() -> None:
    body: dict[str, Any] = {"interaction": {**INTERACTION, "inputId": "other-command"}}
    session, _ = history_session(lambda request, service: json_response(body))
    with pytest.raises(TiDBLinkError, match="invalid interaction receipt"):
        await session.get_interaction("command/1")
    body = {
        "interactions": [{**INTERACTION, "inputId": "other-command"}],
        "hasMore": False,
        "nextCursor": None,
    }
    with pytest.raises(TiDBLinkError, match="invalid interaction receipt"):
        await session.reconcile_interaction("command/1")
    body = {"interactions": None}
    with pytest.raises(TiDBLinkError, match="invalid interaction lookup"):
        await session.reconcile_interaction("command/1")
    body = {"interaction": {**INTERACTION, "result": {"status": "accepted", "completedAt": None}}}
    with pytest.raises(TiDBLinkError, match="invalid interaction receipt"):
        await session.get_interaction("command/1")
    body = {"turn": {**TURN, "sessionId": "another-session"}}
    with pytest.raises(TiDBLinkError, match="different Turn"):
        await session.get_turn("turn_1")


async def test_unavailable_history_and_foreign_cursor_refusals_retain_service_code_and_request_identity() -> None:
    def handler(request, service):
        foreign = "cursor=" in service.calls[-1].path
        return httpx.Response(
            400 if foreign else 404,
            json={
                "error": {
                    "code": "validation_error" if foreign else "session_not_found",
                    "message": "Unavailable",
                }
            },
            headers={"x-request-id": "service-history"},
        )

    session, _ = history_session(handler)
    with pytest.raises(TiDBLinkApiError) as foreign:
        await session.history(cursor="foreign")
    assert (foreign.value.status, foreign.value.code, foreign.value.request_id) == (
        400,
        "validation_error",
        "service-history",
    )
    with pytest.raises(TiDBLinkApiError) as missing:
        await session.timeline()
    assert (missing.value.status, missing.value.code) == (404, "session_not_found")
