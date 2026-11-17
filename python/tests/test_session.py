"""Ported from typescript/test/session.test.mjs."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest

from core_support import disconnect, session_for, user_for
from support import error_response, json_response, session_record
from tidblink_server import ConflictError, OutcomeUnknownError, TiDBLinkApiError

SESSION_PATH = "/api/v1/org/users/user_1/sessions/session_1"
SWITCH_PATH = f"{SESSION_PATH}/template-switches"
CONTRACT = json.loads(
    (Path(__file__).resolve().parents[2] / "typescript" / "service-contract" / "openapi.json").read_text()
)


async def test_an_explicit_template_creates_a_persistent_session_and_retains_its_model_etag() -> None:
    user, service = user_for(
        lambda request, service: json_response({"session": session_record()}, 201, {"etag": '"session-v1"'})
    )
    with pytest.raises(TypeError):
        await user.create_session()  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        await user.create_session(idempotency_key="session-1")  # type: ignore[call-arg]
    assert service.calls == []
    session = await user.create_session(
        template_id="template_1",
        billing_tag={"key": "customer", "value": "acme"},
        model="deployment.model",
        idempotency_key="session-1",
    )
    assert session.id == "session_1"
    assert session.model == "gpt-5.6-terra"
    assert session.data is not None and session.data["template"]["templateId"] == "template_1"
    call = service.calls[0]
    assert call.method == "POST"
    assert call.path == "/api/v1/org/users/user_1/sessions"
    assert call.json() == {
        "agentTemplateId": "template_1",
        "model": "deployment.model",
        "billingTag": {"key": "customer", "value": "acme"},
    }
    assert call.headers["idempotency-key"] == "session-1"
    assert "x-agent9-project-id" not in call.headers
    assert "x-ti-project-id" not in call.headers


async def test_a_session_updates_the_model_with_its_retained_etag_and_never_overwrites_a_conflict() -> None:
    def handler(request, service):
        if request.method == "GET":
            return json_response({"session": session_record()}, headers={"etag": '"session-v1"'})
        if len(service.calls) == 2:
            assert request.headers["if-match"] == '"session-v1"'
            return json_response(
                {"session": session_record(model="claude-sonnet-5", modelRevision=2)},
                headers={"etag": '"session-v2"'},
            )
        assert request.headers["if-match"] == '"session-v2"'
        return error_response(412, "precondition_failed", "Stale Session")

    user, service = user_for(handler)
    session = await user.get_session("session_1")
    await session.set_model("claude-sonnet-5")
    assert session.model == "claude-sonnet-5"
    with pytest.raises(ConflictError):
        await session.set_model("gpt-5.6-sol")
    assert session.model == "claude-sonnet-5"
    assert [(call.method, call.path) for call in service.calls] == [
        ("GET", SESSION_PATH),
        ("PATCH", f"{SESSION_PATH}/model"),
        ("PATCH", f"{SESSION_PATH}/model"),
    ]


async def test_session_metadata_updates_keep_the_independent_model_token_and_do_not_replay_uncertain_effects() -> None:
    def handler(request, service):
        call = service.calls[-1]
        if b"lost" in call.body:
            return disconnect()
        headers = {}
        if request.method == "GET":
            headers["etag"] = '"model-1"'
        if call.path.endswith("/model"):
            assert request.headers["if-match"] == '"model-1"'
            headers["etag"] = '"model-2"'
        return json_response({"session": session_record()}, headers=headers)

    user, service = user_for(handler)
    session = await user.get_session("session_1")
    await session.rename("Named")
    await session.set_billing_tag(None)
    await session.set_model("dynamic-model")
    with pytest.raises(OutcomeUnknownError):
        await session.rename("lost")
    assert len(service.calls) == 5
    assert [(call.method, call.path) for call in service.calls[1:]] == [
        ("POST", f"{SESSION_PATH}/name"),
        ("PATCH", f"{SESSION_PATH}/billing-tag"),
        ("PATCH", f"{SESSION_PATH}/model"),
        ("POST", f"{SESSION_PATH}/name"),
    ]
    assert service.calls[2].json() == {"billingTag": None}


async def test_session_creation_with_exhausted_retries_preserves_the_caller_key_for_explicit_recovery() -> None:
    state = {"fail": True}

    def handler(request, service):
        if state["fail"]:
            return disconnect()
        return json_response({"session": session_record()}, 201, {"etag": '"model-1"'})

    user, service = user_for(handler)
    options = {"template_id": "template_1", "idempotency_key": "session-recover"}
    with pytest.raises(OutcomeUnknownError):
        await user.create_session(**options)
    state["fail"] = False
    assert (await user.create_session(**options)).id == "session_1"
    assert all(call.headers["idempotency-key"] == "session-recover" for call in service.calls)


async def test_session_suggested_naming_keeps_the_model_token_and_reports_uncertain_naming_without_replay() -> None:
    state = {"suggests": 0}

    def handler(request, service):
        call = service.calls[-1]
        if request.method == "GET":
            return json_response({"session": session_record()}, headers={"etag": '"model-1"'})
        if call.path.endswith("/name/suggest"):
            state["suggests"] += 1
            assert call.json() == {"userText": "Help plan", "assistantText": "A plan"}
            if state["suggests"] > 1:
                return disconnect()
            return json_response({"name": "Planning", "session": session_record(name="Planning")})
        assert request.headers["if-match"] == '"model-1"'
        return json_response({"session": session_record()}, headers={"etag": '"model-2"'})

    user, _ = user_for(handler)
    session = await user.get_session("session_1")
    await session.suggest_name(user_text="Help plan", assistant_text="A plan")
    assert session.data is not None and session.data["name"] == "Planning"
    await session.set_model("dynamic-model")
    with pytest.raises(OutcomeUnknownError):
        await session.suggest_name(user_text="Help plan", assistant_text="A plan")
    assert state["suggests"] == 2


def switch_receipt(**overrides: Any) -> dict[str, Any]:
    return {
        "commandId": "stsw_1",
        "sessionId": "session_1",
        "agentTemplateId": "template_2",
        "status": "pending",
        "configurationEpoch": 1,
        "handoffTurnId": "turn_handoff",
        "createdAt": "2026-11-12T00:00:00.000Z",
        **overrides,
    }


def same_key_hint(error: BaseException) -> bool:
    return (
        isinstance(error, OutcomeUnknownError)
        and error.session_id == "session_1"
        and "same idempotency_key" in str(error)
    )


_JSON_TYPES = {str: "string", bool: "boolean", int: "number", float: "number"}


async def test_a_template_switch_validates_locally_and_sends_the_body_and_key_the_frozen_contract_accepts() -> None:
    session, service = session_for(lambda request, service: json_response({"receipt": switch_receipt()}, 202))
    for options, expected in [
        ({}, TypeError),
        ({"template_id": "template_2"}, TypeError),
        ({"idempotency_key": "switch-1"}, TypeError),
        ({"template_id": " template_2", "idempotency_key": "switch-1"}, ValueError),
        ({"template_id": "t" * 65, "idempotency_key": "switch-1"}, ValueError),
        ({"template_id": "template_2", "idempotency_key": "switch 1"}, ValueError),
        ({"template_id": "template_2", "idempotency_key": "_switch"}, ValueError),
        ({"template_id": "template_2", "idempotency_key": "k" * 129}, ValueError),
        ({"template_id": "template_2", "idempotency_key": "switch-1", "acknowledge_session_recall": "yes"}, TypeError),
        ({"agent_template_id": "template_2", "idempotency_key": "switch-1"}, TypeError),
    ]:
        with pytest.raises(expected):
            await session.switch_template(**options)
    with pytest.raises(ValueError, match=r"^Template switch requires a stable idempotency_key"):
        await session.switch_template(template_id="template_2", idempotency_key="_x")
    assert service.calls == []

    longest = "k" * 128
    receipt = await session.switch_template(
        template_id="template_2",
        idempotency_key=longest,
        acknowledge_session_recall=True,
        request_id="request-1",
    )
    assert receipt == switch_receipt()
    await session.switch_template(template_id="template_2", idempotency_key="switch-2")

    calls = service.calls
    assert [call.path for call in calls] == [SWITCH_PATH, SWITCH_PATH]
    assert calls[0].json() == {"agentTemplateId": "template_2", "acknowledgeSessionRecall": True}
    assert calls[0].headers["idempotency-key"] == longest
    assert calls[0].headers["x-request-id"] == "request-1"
    assert calls[1].json() == {"agentTemplateId": "template_2"}

    # Every body the SDK sent fits the frozen request schema and key grammar.
    operation = CONTRACT["paths"]["/api/v1/org/users/{userId}/sessions/{id}/template-switches"]["post"]
    schema = CONTRACT["components"]["schemas"]["SwitchSessionTemplateOpenApiDto"]
    key_pattern = re.compile(
        next(p for p in operation["parameters"] if p["name"] == "Idempotency-Key")["schema"]["pattern"]
    )
    for call in calls:
        body = call.json()
        for name in schema["required"]:
            assert name in body, name
        for name, value in body.items():
            declared = schema["properties"].get(name)
            assert declared, f"undeclared field {name}"
            assert _JSON_TYPES[type(value)] == declared["type"]
        assert key_pattern.search(call.headers["idempotency-key"])


async def test_a_template_switch_replays_its_command_latest_receipt_by_key_reuse_refused_replay_before_busy() -> None:
    # Modelled on the service's switch command store: keyed by Idempotency-Key and
    # request hash, the replay answered before the in-progress and busy checks.
    commands: dict[str, dict[str, Any]] = {}
    bodies: dict[str, list[bytes]] = {}
    state: dict[str, Any] = {"fence": None, "epoch": 1}

    def handler(request, service):
        call = service.calls[-1]
        key = call.headers["idempotency-key"]
        bodies.setdefault(key, []).append(call.body)
        body = call.json()
        digest = json.dumps(["session_1", body["agentTemplateId"], body.get("acknowledgeSessionRecall") is True])
        prior = commands.get(key)
        if prior:
            if prior["hash"] != digest:
                return error_response(409, "idempotency_key_reused", "Reused")
            return json_response({"receipt": prior["receipt"]}, 202)
        if state["fence"]:
            return error_response(409, "session_switch_in_progress", "Switching")
        receipt = switch_receipt(
            commandId=f"stsw_{len(commands) + 1}",
            agentTemplateId=body["agentTemplateId"],
            configurationEpoch=state["epoch"],
            handoffTurnId=f"turn_handoff_{len(commands) + 1}",
        )
        commands[key] = {"hash": digest, "receipt": receipt}
        state["fence"] = key
        return json_response({"receipt": receipt}, 202)

    def settle() -> None:
        command = commands[state["fence"]]
        state["epoch"] += 1
        command["receipt"] = {
            **command["receipt"],
            "status": "succeeded",
            "configurationEpoch": state["epoch"],
            "completedAt": "2026-11-12T00:00:05.000Z",
        }
        state["fence"] = None

    session, _ = session_for(handler)
    options = {"template_id": "template_2", "idempotency_key": "switch-1"}

    pending = await session.switch_template(**options)
    assert pending["status"] == "pending"
    assert pending["handoffTurnId"] == "turn_handoff_1"
    assert await session.switch_template(**options) == pending
    with pytest.raises(ConflictError) as busy:
        await session.switch_template(template_id="template_3", idempotency_key="switch-2")
    assert busy.value.code == "session_switch_in_progress"
    settle()
    settled = await session.switch_template(**options)
    assert settled == {
        **pending,
        "status": "succeeded",
        "configurationEpoch": 2,
        "completedAt": "2026-11-12T00:00:05.000Z",
    }
    with pytest.raises(ConflictError) as reused:
        await session.switch_template(**{**options, "template_id": "template_3"})
    assert reused.value.status == 409
    assert reused.value.code == "idempotency_key_reused"
    replays = bodies["switch-1"]
    assert len(replays) == 4
    assert len(set(replays[:3])) == 1


async def test_an_uncertain_template_switch_names_its_session_keeps_its_key_and_is_recovered_by_repeating() -> None:
    state = {"drops": 4, "hold": False}
    held = asyncio.Event()

    async def handler(request, service):
        if state["hold"]:
            await held.wait()
        if state["drops"] > 0:
            state["drops"] -= 1
            return disconnect()
        return json_response(
            {
                "receipt": switch_receipt(
                    status="succeeded", configurationEpoch=2, completedAt="2026-11-12T00:00:01.000Z"
                )
            },
            202,
        )

    session, service = session_for(handler)
    options = {"template_id": "template_2", "idempotency_key": "switch-recover"}
    keys = lambda: [call.headers["idempotency-key"] for call in service.calls]  # noqa: E731

    with pytest.raises(OutcomeUnknownError) as caught:
        await session.switch_template(**options)
    assert same_key_hint(caught.value)
    assert len(keys()) == 3
    # One more dropped response is retried automatically with the same key.
    receipt = await session.switch_template(**options)
    assert receipt["status"] == "succeeded"
    assert len(keys()) == 5
    assert all(key == "switch-recover" for key in keys())

    # TypeScript aborts the dispatched switch with a signal and gets the
    # same-key hint; in Python the caller cancels, and that propagates unchanged.
    state["hold"] = True
    sent = asyncio.create_task(session.switch_template(**options))
    while len(keys()) < 6:
        await asyncio.sleep(0.005)
    sent.cancel()
    with pytest.raises(asyncio.CancelledError):
        await sent


async def test_pre_recording_503_refusals_are_definite_and_other_server_failures_stay_uncertain() -> None:
    codes = [""]
    session, _ = session_for(lambda request, service: error_response(503, codes[0], "Unavailable"))
    options = {"template_id": "template_2", "idempotency_key": "switch-1"}
    for code in ["turn_admission_closed", "skill_package_store_unavailable"]:
        codes[0] = code
        with pytest.raises(TiDBLinkApiError) as caught:
            await session.switch_template(**options)
        assert not isinstance(caught.value, OutcomeUnknownError)
        assert caught.value.status == 503
        assert caught.value.code == code
    codes[0] = "service_unavailable"
    with pytest.raises(OutcomeUnknownError) as uncertain:
        await session.switch_template(**options)
    assert same_key_hint(uncertain.value)


async def test_template_switch_receipts_are_returned_as_is_per_status_and_unusable_ones_are_uncertain() -> None:
    replies: list[tuple[int, Any]] = []

    def handler(request, service):
        status, body = replies.pop(0)
        return json_response(body, status)

    session, _ = session_for(handler)
    options = {"template_id": "template_2", "idempotency_key": "switch-1"}

    replies.append((409, {"error": {"code": "session_switch_busy", "message": "Busy"}}))
    with pytest.raises(ConflictError) as busy:
        await session.switch_template(**options)
    assert busy.value.status == 409 and busy.value.code == "session_switch_busy"
    replies.append((404, {"error": {"code": "agent_template_unavailable", "message": "Gone"}}))
    with pytest.raises(TiDBLinkApiError) as gone:
        await session.switch_template(**options)
    assert not isinstance(gone.value, ConflictError)
    assert gone.value.status == 404 and gone.value.code == "agent_template_unavailable"

    without_handoff = {k: v for k, v in switch_receipt().items() if k != "handoffTurnId"}
    for receipt in [
        switch_receipt(),
        {**without_handoff, "status": "unchanged"},
        {
            **switch_receipt(),
            "status": "failed",
            "completedAt": "2026-11-12T00:00:05.000Z",
            "error": {"code": "session_switch_conflict"},
        },
        {
            **without_handoff,
            "status": "succeeded",
            "configurationEpoch": 2,
            "completedAt": "2026-11-12T00:00:05.000Z",
        },
    ]:
        replies.append((202, {"receipt": receipt}))
        assert await session.switch_template(**options) == receipt

    without_created_at = {k: v for k, v in switch_receipt().items() if k != "createdAt"}
    for body in [
        {},
        {"receipt": switch_receipt(sessionId="session_2")},
        {"receipt": switch_receipt(agentTemplateId="template_3")},
        {"receipt": switch_receipt(commandId="")},
        {"receipt": switch_receipt(status="running")},
        {"receipt": without_created_at},
        {"receipt": switch_receipt(configurationEpoch=0)},
        {"receipt": switch_receipt(configurationEpoch=1.5)},
        {"receipt": without_handoff},
        {"receipt": switch_receipt(status="failed")},
        {"receipt": switch_receipt(status="failed", error={})},
        {"receipt": switch_receipt(status="unchanged")},
    ]:
        replies.append((202, body))
        with pytest.raises(OutcomeUnknownError) as caught:
            await session.switch_template(**options)
        assert same_key_hint(caught.value)
    assert replies == []
