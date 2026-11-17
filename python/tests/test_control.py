"""Ported from typescript/test/control.test.mjs."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from core_support import disconnect, session_for
from support import error_response, json_response
from tidblink_server import OutcomeUnknownError, TiDBLinkApiError

COMPLETED_AT = "2026-10-28T00:00:02Z"


def receipt(kind: str, **overrides: Any) -> dict[str, Any]:
    return {
        "inputId": "command_1",
        "sequence": 2,
        "kind": kind,
        "mode": "cancelled" if kind == "cancel" else "started",
        "turnId": "turn_1",
        "message": None,
        "createdAt": "2026-10-28T00:00:00Z",
        "appliedAt": None,
        "rejection": None,
        "result": {"status": "running", "completedAt": None},
        **overrides,
    }


def retry_event(**overrides: Any) -> dict[str, Any]:
    return {
        "clientEventId": "command_1",
        "sessionId": "session_1",
        "turnId": "turn_1",
        "disposition": "retrying",
        **overrides,
    }


async def test_cancellation_targets_one_turn_with_stable_identity_and_reports_acceptance_before_termination() -> None:
    session, service = session_for(lambda request, service: json_response({"interaction": receipt("cancel")}, 202))
    accepted = await session.cancel(client_event_id="command_1", turn_id="turn_1", request_id="request_1")
    assert accepted["result"]["status"] == "running"
    assert [
        (call.method, call.path, call.json(), call.headers["x-request-id"]) for call in service.calls
    ] == [
        (
            "POST",
            "/api/v1/org/users/user_1/sessions/session_1/events",
            {"type": "agent.session.input.cancel", "clientEventId": "command_1", "data": {"turnId": "turn_1"}},
            "request_1",
        )
    ]


async def test_explicit_retry_keeps_its_own_receipt_and_wait_result_while_the_original_remains_interrupted() -> None:
    original = receipt(
        "message",
        inputId="original",
        mode="started",
        appliedAt="2026-10-28T00:00:00Z",
        message={"id": "user_1", "role": "user", "content": "Hello", "createdAt": "2026-10-28T00:00:00Z"},
        result={"status": "interrupted", "completedAt": "2026-10-28T00:00:01Z"},
    )
    retry = receipt("retry", result={"status": "succeeded", "completedAt": COMPLETED_AT})

    def handler(request, service):
        path = request.url.path
        if request.method == "POST":
            return json_response({"event": retry_event()}, 202)
        if path.endswith("/interactions/original"):
            return json_response({"interaction": original})
        if path.endswith("/interactions/command_1"):
            return json_response({"interaction": retry})
        return json_response(
            {
                "turn": {
                    "id": "turn_1",
                    "sessionId": "session_1",
                    "status": "succeeded",
                    "completedAt": COMPLETED_AT,
                    "assistantMessage": {
                        "id": "answer_1",
                        "sessionId": "session_1",
                        "turnId": "turn_1",
                        "role": "assistant",
                        "content": "Retried answer",
                        "createdAt": COMPLETED_AT,
                    },
                }
            }
        )

    session, service = session_for(handler)
    admitted = await session.retry(client_event_id="command_1", turn_id="turn_1")
    assert admitted["disposition"] == "retrying"
    commands = [call.json() for call in service.calls if call.method == "POST"]
    assert commands == [
        {"type": "agent.session.input.retry", "clientEventId": "command_1", "data": {"turnId": "turn_1"}}
    ]
    result = await session.wait_for_result(admitted["clientEventId"])
    assert result.text == "Retried answer"
    assert result.input_id == "command_1"
    assert await session.get_interaction("original") == original
    assert len([call for call in service.calls if call.method == "POST"]) == 1


async def test_control_uncertainty_retains_the_exact_command_and_never_repeats_effects() -> None:
    for method in ["cancel", "retry"]:
        for response in ["disconnect", "unusable", "wrong-target", "server-error"]:

            def handler(request, service, method=method, response=response):
                if response == "disconnect":
                    return disconnect()
                if response == "server-error":
                    return error_response(503, "service_unavailable", "Unknown command outcome")
                if response == "wrong-target":
                    return json_response(
                        {"interaction": receipt("cancel", turnId="other")}
                        if method == "cancel"
                        else {"event": retry_event(sessionId="other")}
                    )
                return json_response({})

            session, service = session_for(handler)
            with pytest.raises(OutcomeUnknownError) as caught:
                await getattr(session, method)(client_event_id="command_1", turn_id="turn_1")
            error = caught.value
            assert (error.input_id, error.turn_id, error.session_id) == ("command_1", "turn_1", "session_1")
            assert len(service.calls) == 1, (method, response)


async def test_refused_controls_preserve_the_service_error_and_cannot_substitute_a_new_message() -> None:
    for method, status, code in [
        ("retry", 400, "checkpoint_runtime_unavailable"),
        ("cancel", 409, "interaction_admission_closed"),
        ("retry", 403, "forbidden"),
        ("cancel", 404, "session_not_found"),
    ]:
        session, service = session_for(
            lambda request, service, status=status, code=code: error_response(
                status, code, "Refused", details={"target": "turn_1"}, requestId="refusal_1"
            )
        )
        with pytest.raises(TiDBLinkApiError) as caught:
            await getattr(session, method)(client_event_id="command_1", turn_id="turn_1")
        error = caught.value
        assert (error.code, error.status, error.request_id) == (code, status, "refusal_1")
        assert error.details["target"] == "turn_1"
        assert len(service.calls) == 1
        assert service.calls[0].json()["type"] == f"agent.session.input.{method}"


async def test_invalid_control_identities_and_commands_cancelled_before_dispatch_perform_no_request() -> None:
    session, service = session_for(lambda request, service: json_response({}))
    for method in ["cancel", "retry"]:
        for options, expected in [
            ({"turn_id": "turn_1"}, TypeError),
            ({"client_event_id": "command_1"}, TypeError),
            ({"client_event_id": "command_1", "turn_id": ""}, ValueError),
            ({"client_event_id": "command_1", "turn_id": "turn_1", "obsolete": True}, TypeError),
        ]:
            with pytest.raises(expected):
                await getattr(session, method)(**options)
        # An already aborted signal in TypeScript; a task cancelled before it runs in Python.
        task = asyncio.create_task(getattr(session, method)(client_event_id="command_1", turn_id="turn_1"))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert service.calls == []
