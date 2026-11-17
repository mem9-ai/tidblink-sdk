"""Ported from typescript/test/turn.test.mjs."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from core_support import disconnect, session_for
from support import error_response, json_response
from tidblink_server import (
    InputNotAppliedError,
    OutcomeUnknownError,
    TurnFailedError,
    TurnInterruptedError,
    TurnWaitTimeoutError,
)

SESSION_PATH = "/api/v1/org/users/user_1/sessions/session_1"
COMPLETED_AT = "2026-10-28T00:00:01Z"


def receipt(**overrides: Any) -> dict[str, Any]:
    return {
        "inputId": "input_1",
        "sequence": 1,
        "kind": "message",
        "mode": "started",
        "turnId": "turn_1",
        "message": {"id": "user_1", "role": "user", "content": "Hello", "createdAt": "2026-10-28T00:00:00Z"},
        "createdAt": "2026-10-28T00:00:00Z",
        "appliedAt": None,
        "rejection": None,
        "result": {"status": "running", "completedAt": None},
        **overrides,
    }


def final_receipt(**overrides: Any) -> dict[str, Any]:
    return receipt(
        **{
            "appliedAt": "2026-10-28T00:00:00Z",
            "result": {"status": "succeeded", "completedAt": COMPLETED_AT},
            **overrides,
        }
    )


def assistant_message(**overrides: Any) -> dict[str, Any]:
    return {
        "id": "assistant_1",
        "role": "assistant",
        "content": "Done",
        "sessionId": "session_1",
        "turnId": "turn_1",
        "createdAt": COMPLETED_AT,
        **overrides,
    }


def saved_turn(**overrides: Any) -> dict[str, Any]:
    return {
        "id": "turn_1",
        "sessionId": "session_1",
        "status": "succeeded",
        "billingTag": None,
        "userMessage": {
            "id": "user_1",
            "role": "user",
            "content": "Hello",
            "sessionId": "session_1",
            "turnId": "turn_1",
            "createdAt": "2026-10-28T00:00:00Z",
        },
        "operations": [],
        "assistantMessage": assistant_message(),
        "createdAt": "2026-10-28T00:00:00Z",
        "completedAt": COMPLETED_AT,
        **overrides,
    }


def answering(turn: dict[str, Any], interaction: dict[str, Any] | None = None):
    def handler(request, service):
        if request.url.path.endswith("/turns/turn_1"):
            return json_response({"turn": turn})
        return json_response({"interaction": interaction or final_receipt()})

    return handler


async def test_text_submission_exposes_acceptance_with_the_callers_identity_and_does_not_wait() -> None:
    session, service = session_for(lambda request, service: json_response({"interaction": receipt()}, 202))
    accepted = await session.submit_message(client_event_id="input_1", text="Hello", request_id="http_1")
    assert accepted == receipt()
    assert len(service.calls) == 1
    call = service.calls[0]
    assert call.path == f"{SESSION_PATH}/events"
    assert call.headers["x-request-id"] == "http_1"
    assert call.json() == {
        "type": "agent.session.input.message",
        "clientEventId": "input_1",
        "data": {"text": "Hello"},
    }


async def test_waiting_polls_the_original_interaction_until_incorporation_and_success_before_reading_the_answer() -> None:
    polls = {"count": 0}

    def handler(request, service):
        if request.url.path.endswith("/turns/turn_1"):
            return json_response({"turn": saved_turn()})
        polls["count"] += 1
        return json_response({"interaction": receipt() if polls["count"] == 1 else final_receipt()})

    session, service = session_for(handler)
    result = await session.wait_for_result("input_1", poll_interval=0.001)
    assert result.type == "answer"
    assert result.text == "Done"
    assert result.input_id == "input_1"
    assert result.session_id == "session_1"
    assert result.interaction == final_receipt()
    assert [(call.method, call.path) for call in service.calls] == [
        ("GET", f"{SESSION_PATH}/interactions/input_1"),
        ("GET", f"{SESSION_PATH}/interactions/input_1"),
        ("GET", f"{SESSION_PATH}/turns/turn_1"),
    ]


async def test_uncertain_submissions_preserve_both_identities_and_never_repeat_a_post() -> None:
    responses = {
        "disconnect": disconnect,
        "empty": lambda: json_response({}),
        "mismatched": lambda: json_response({"interaction": receipt(inputId="other")}),
        "server": lambda: error_response(503, "server_error", "uncertain"),
        "eof": lambda: httpx.Response(202, headers={"content-type": "application/x-ndjson"}, content=b""),
    }
    for mode, respond in responses.items():
        session, service = session_for(lambda request, service, respond=respond: respond())
        with pytest.raises(OutcomeUnknownError) as caught:
            await session.submit_message(client_event_id="input_1", text="Hello", request_id="http_1")
        error = caught.value
        assert (error.session_id, error.input_id, error.request_id) == ("session_1", "input_1", "http_1"), mode
        assert len(service.calls) == 1, mode


async def test_local_timeout_and_cancellation_keep_the_original_identity_without_cancelling_or_resubmitting() -> None:
    session, service = session_for(lambda request, service: json_response({"interaction": receipt()}))
    with pytest.raises(TurnWaitTimeoutError) as caught:
        await session.wait_for_result("input_1", timeout=0.015, request_id="http_wait", poll_interval=0.002)
    error = caught.value
    assert (error.input_id, error.session_id, error.request_id) == ("input_1", "session_1", "http_wait")
    # TypeScript aborts the wait with a signal and gets OutcomeUnknownError; in
    # Python the caller cancels the task, and the cancellation propagates unchanged.
    task = asyncio.create_task(session.wait_for_result("input_1", timeout=0.5, poll_interval=0.002))
    await asyncio.sleep(0.015)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert service.calls
    assert all(
        call.method == "GET" and call.path.endswith("/interactions/input_1") for call in service.calls
    )


async def test_failed_and_interrupted_receipts_retain_the_original_outcome_after_a_later_execution() -> None:
    for status, error_class in [("failed", TurnFailedError), ("interrupted", TurnInterruptedError)]:
        session, service = session_for(
            answering(saved_turn(), final_receipt(result={"status": status, "completedAt": COMPLETED_AT}))
        )
        with pytest.raises(error_class) as caught:
            await session.wait_for_result("input_1")
        error = caught.value
        assert (error.input_id, error.session_id, error.turn_id) == ("input_1", "session_1", "turn_1")
        assert len(service.calls) == 1


async def test_a_succeeded_turn_cannot_establish_that_an_unapplied_or_refused_message_succeeded() -> None:
    for overrides in [
        {"mode": "steered", "appliedAt": None},
        {"mode": "rejected", "rejection": {"code": "interaction_admission_closed"}},
    ]:
        session, service = session_for(answering(saved_turn(), final_receipt(**overrides)))
        with pytest.raises(InputNotAppliedError):
            await session.wait_for_result("input_1")
        assert len(service.calls) == 1


async def test_a_final_answer_must_belong_to_the_original_execution() -> None:
    for overrides in [
        {"completedAt": "2026-10-28T00:02:00Z"},
        {"status": "running"},
        {"id": "other"},
        {"assistantMessage": None},
        {"assistantMessage": assistant_message(id=42)},
        {"assistantMessage": assistant_message(turnId="other")},
    ]:
        session, _ = session_for(answering(saved_turn(**overrides)))
        with pytest.raises(OutcomeUnknownError):
            await session.wait_for_result("input_1")


async def test_linked_clarification_inputs_and_saved_clarification_answers_retain_their_structured_values() -> None:
    clarification = {
        "prompt": "Choose",
        "selectionMode": "multiple",
        "answerChoices": ["A", "B"],
        "response": {"answers": ["B", "custom"], "responseTurnId": "turn_2"},
    }

    def handler(request, service):
        if request.method == "POST":
            return json_response({"interaction": final_receipt()}, 202)
        return answering(saved_turn(clarificationItem=clarification))(request, service)

    session, service = session_for(handler)
    for text in ["A", ["A"], ["B", "custom"]]:
        result = await session.turn(
            client_event_id="input_1",
            text=text,
            clarification_source_turn_id="source",
            billing_tag=None,
            user_file_ids=["file_1"],
        )
        assert result.type == "clarification"
        assert result.clarification == clarification
        posted = [call for call in service.calls if call.method == "POST"]
        assert posted[-1].json()["data"] == {
            "text": text,
            "clarificationSourceTurnId": "source",
            "billingTag": None,
            "userFileIds": ["file_1"],
        }


async def test_missing_command_identities_and_unsupported_options_fail_before_dispatch() -> None:
    session, service = session_for(lambda request, service: json_response({}))
    for options, expected in [
        ({"text": "Hello"}, TypeError),
        ({"client_event_id": "invalid key", "text": "Hello"}, ValueError),
        ({"client_event_id": "input_1", "text": "Hello", "output_schema": {}}, TypeError),
        ({"client_event_id": "input_1", "text": []}, ValueError),
        ({"client_event_id": "input_1", "text": "Hello", "retry": "safe"}, TypeError),
    ]:
        with pytest.raises(expected):
            await session.submit_message(**options)
    assert service.calls == []


async def test_invalid_local_wait_settings_cannot_dispatch_a_message() -> None:
    session, service = session_for(lambda request, service: json_response({"interaction": receipt()}, 202))
    for options, expected in [
        ({"timeout": -1}, ValueError),
        ({"poll_interval": 0}, ValueError),
        ({"timeout": "60"}, TypeError),
        ({"retry": "safe"}, TypeError),
    ]:
        with pytest.raises(expected):
            await session.turn(client_event_id="input_1", text="Hello", **options)
    assert service.calls == []


async def test_stopping_a_dispatched_submission_does_not_repeat_it() -> None:
    # TypeScript aborts the dispatched request and gets OutcomeUnknownError with
    # the command identity; in Python the cancellation propagates unchanged.
    started = asyncio.Event()

    async def handler(request, service):
        started.set()
        await asyncio.Event().wait()

    session, service = session_for(handler)
    task = asyncio.create_task(
        session.submit_message(client_event_id="input_1", text="Hello", request_id="http_1")
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(service.calls) == 1


class _Endless(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'{"interaction":'
        await asyncio.Event().wait()


async def test_the_wait_deadline_also_interrupts_a_response_whose_body_never_completes() -> None:
    session, _ = session_for(
        lambda request, service: httpx.Response(
            200, headers={"content-type": "application/json"}, stream=_Endless()
        )
    )
    with pytest.raises(TurnWaitTimeoutError) as caught:
        await session.wait_for_result("input_1", timeout=0.02)
    assert caught.value.input_id == "input_1"


async def test_a_malformed_saved_clarification_cannot_become_a_successful_typed_result() -> None:
    for item in [
        {},
        {"prompt": "Choose", "selectionMode": "impossible", "answerChoices": ["A", "B"]},
        {"prompt": "", "selectionMode": "single", "answerChoices": ["A", "B"]},
        {"prompt": "Choose", "selectionMode": "single", "answerChoices": ["A", "B", "C", "D"]},
        {
            "prompt": "Choose",
            "selectionMode": "multiple",
            "answerChoices": ["A", "B"],
            "response": {"answers": [], "responseTurnId": "turn_2"},
        },
    ]:
        session, _ = session_for(answering(saved_turn(clarificationItem=item)))
        with pytest.raises(OutcomeUnknownError):
            await session.wait_for_result("input_1")


async def test_turn_waiting_stays_bound_to_its_submitted_identity() -> None:
    # TypeScript mutates the caller's input object after dispatch; Python takes
    # keyword arguments, so this checks the wait reads the submitted identity.
    def handler(request, service):
        if request.method == "POST":
            return json_response({"interaction": final_receipt()}, 202)
        return answering(saved_turn())(request, service)

    session, service = session_for(handler)
    result = await session.turn(client_event_id="input_1", text="Hello")
    assert result.input_id == "input_1"
    reads = [call.path for call in service.calls if call.method == "GET"]
    assert reads[0] == f"{SESSION_PATH}/interactions/input_1"


async def test_turn_sends_its_request_id_only_with_the_submission() -> None:
    def handler(request, service):
        if request.method == "POST":
            return json_response({"interaction": final_receipt()}, 202)
        return answering(saved_turn())(request, service)

    session, service = session_for(handler)
    await session.turn(client_event_id="input_1", text="Hello", request_id="http_submit")
    post, *reads = service.calls
    assert post.headers["x-request-id"] == "http_submit"
    assert reads and all(call.headers["x-request-id"] != "http_submit" for call in reads)
