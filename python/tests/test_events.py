"""Port of ``typescript/test/events.test.mjs``."""

from __future__ import annotations

from typing import Any

import pytest

from tidblink_server.errors import TiDBLinkError
from tidblink_server.events import as_known_session_event, session_event_from

CREATED_AT = "2026-11-11T00:00:00.000Z"


def turn_data(event: str, payload: dict[str, Any], turn_id: str = "turn_1") -> dict[str, Any]:
    return {"event": event, "turnId": turn_id, "sessionId": "session_1", "seq": 1, "createdAt": CREATED_AT, "payload": payload}


def envelope(type_: str, data: dict[str, Any], turn_id: str | None = "turn_1") -> dict[str, Any]:
    return {"type": type_, "sessionId": "session_1", "turnId": turn_id, "createdAt": CREATED_AT, "data": data}


def tool_calls(*ids: str) -> dict[str, Any]:
    return envelope(
        "agent.session.turn.requires_action",
        turn_data(
            "client_tool_calls",
            {
                "calls": [
                    {
                        "callId": call_id,
                        "name": "lookup_order",
                        "arguments": {"orderId": "order_1"},
                        "deadlineAt": "2099-01-01T00:00:00.000Z",
                    }
                    for call_id in ids
                ]
            },
        ),
    )


SAMPLES: dict[str, dict[str, Any]] = {
    "agent.session.turn.started": turn_data("turn_started", {"inputId": "input_1", "execution": {"model": "model-a"}}),
    "agent.session.turn.output_text.snapshot": turn_data("assistant_draft", {"text": "Draft"}),
    "agent.session.turn.progress": turn_data("progress", {"text": "Searching"}),
    "agent.session.turn.item.added": turn_data("assistant_message", {"messageId": "message_1", "text": "Done"}),
    "agent.session.turn.requires_action": tool_calls("call_1")["data"],
    "agent.session.turn.completed": turn_data("turn_finished", {"status": "succeeded"}),
    "agent.session.turn.cancelled": turn_data("turn_finished", {"status": "interrupted"}),
    "agent.session.turn.failed": turn_data("turn_error", {"code": "model_error", "message": "The model failed"}),
    "agent.session.realtime.input_transcript.completed": {
        "id": "rt_1",
        "type": "input_transcript",
        "realtimeConnectionId": "rtc_1",
        "text": "Hello",
        "turnId": None,
        "createdAt": CREATED_AT,
    },
    "agent.session.realtime.response.completed": {
        "id": "rt_2",
        "type": "response",
        "realtimeConnectionId": "rtc_1",
        "text": "Hi",
        "turnId": None,
        "createdAt": CREATED_AT,
        "kind": "chat",
        "responseId": "resp_1",
        "interrupted": False,
    },
    "agent.session.idle": {"id": "idle_1", "turnId": "turn_1", "createdAt": CREATED_AT},
    "agent.session.template_switch.succeeded": {
        "id": "switch_1",
        "commandId": "command_1",
        "status": "succeeded",
        "configurationEpoch": 2,
        "agentTemplateId": "tmpl_2",
        "contentDigest": "a" * 64,
        "createdAt": CREATED_AT,
    },
}


def with_cursor(cursor: str, value: dict[str, Any]) -> Any:
    return {**value, "cursor": cursor}


def test_every_known_event_type_narrows_to_its_typed_form_and_other_events_pass_through() -> None:
    for type_, data in SAMPLES.items():
        turn_id = "turn_1" if type_.startswith("agent.session.turn.") else None
        event = with_cursor(f"cursor-{type_}", envelope(type_, data, turn_id))
        known = as_known_session_event(event)
        assert known is not None and known["type"] == type_, type_
    operation = with_cursor(
        "c",
        envelope(
            "agent.session.turn.item.added",
            turn_data(
                "operation_step",
                {"operationId": "op_1", "status": "running", "label": "lookup_order", "source": {"type": "session_client"}},
            ),
        ),
    )
    assert as_known_session_event(operation)
    finished_failed = with_cursor(
        "c", envelope("agent.session.turn.failed", turn_data("turn_finished", {"status": "failed"}))
    )
    assert as_known_session_event(finished_failed)
    future = with_cursor("c", envelope("agent.session.future.thing", {"anything": 1}, None))
    assert as_known_session_event(future) is None
    # A known type with a different shape is not narrowed.
    mismatched = with_cursor(
        "c", envelope("agent.session.turn.completed", turn_data("turn_finished", {"status": "failed"}))
    )
    assert as_known_session_event(mismatched) is None
    idle_without_turn = with_cursor(
        "c", envelope("agent.session.idle", {"id": "idle_2", "turnId": None, "createdAt": CREATED_AT}, None)
    )
    known = as_known_session_event(idle_without_turn)
    assert known is not None and known["type"] == "agent.session.idle"


def test_session_event_from_checks_the_envelope_and_attaches_the_cursor() -> None:
    value = envelope("agent.session.future.thing", {"anything": 1}, None)
    assert session_event_from(value, "7", session_id="session_1") == {**value, "cursor": "7"}
    for bad_value, cursor, session_id in [
        (value, "", None),
        (value, 7, None),
        ({**value, "data": []}, "7", None),
        ({k: v for k, v in value.items() if k != "turnId"}, "7", None),
        (value, "7", "session_2"),
    ]:
        with pytest.raises(TiDBLinkError, match="invalid envelope"):
            session_event_from(bad_value, cursor, session_id=session_id)
