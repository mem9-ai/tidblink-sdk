"""Ported from typescript/test/end-user-client.test.mjs."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from core_support import KEY, LocalServer, disconnect, user_for
from examples.create_session import create_session_workflow
from support import error_response, json_response, serve
from tidblink_server import OrganizationClient, OutcomeUnknownError


def created(request: httpx.Request | None = None, service: object = None) -> httpx.Response:
    return json_response({"session": {"sessionId": "session_1"}}, 201, {"etag": '"model-1"'})


async def test_session_creation_recovers_through_the_callers_stable_identity() -> None:
    def handler(request, service):
        return disconnect() if len(service.calls) == 1 else created()

    user, service = user_for(handler)
    session = await user.create_session(template_id="template_1", idempotency_key="create-session-1")
    assert session.id == "session_1"
    assert len(service.calls) == 2
    assert service.calls[0].headers["x-request-id"] == service.calls[1].headers["x-request-id"]
    for call in service.calls:
        assert call.path == "/api/v1/org/users/user_1/sessions"
        assert call.headers["idempotency-key"] == "create-session-1"
        assert call.json() == {"agentTemplateId": "template_1"}


async def test_exhausted_session_creation_remains_recoverable_with_the_callers_key() -> None:
    state = {"fail": True}

    def handler(request, service):
        return disconnect() if state["fail"] else created()

    user, service = user_for(handler)
    with pytest.raises(OutcomeUnknownError):
        await user.create_session(template_id="template_1", idempotency_key="recover-1")
    state["fail"] = False
    assert (await user.create_session(template_id="template_1", idempotency_key="recover-1")).id == "session_1"
    assert all(call.headers["idempotency-key"] == "recover-1" for call in service.calls)


async def test_session_creation_sends_the_session_options_that_replaced_agent_preferences() -> None:
    user, service = user_for(created)
    await user.create_session(
        template_id="template_1",
        idempotency_key="options-1",
        additional_instructions="Use metric units.",
        sandbox={"profileId": None, "workingDirectory": "work"},
        acknowledge_session_recall=True,
        realtime_model="voice-a",
        realtime_voice="calm",
    )
    assert [call.json() for call in service.calls] == [
        {
            "agentTemplateId": "template_1",
            "additionalInstructions": "Use metric units.",
            "sandbox": {"profileId": None, "workingDirectory": "work"},
            "acknowledgeSessionRecall": True,
            "realtimeModel": "voice-a",
            "realtimeVoice": "calm",
        }
    ]


async def test_session_creation_sends_explicit_nulls_and_reports_a_receipt_without_a_model_etag_as_unknown() -> None:
    state = {"etag": True}

    def handler(request, service):
        if state["etag"]:
            return created()
        return json_response({"session": {"sessionId": "session_1"}}, 201)

    user, service = user_for(handler)
    options = dict(
        template_id="template_1",
        idempotency_key="nulls-1",
        additional_instructions=None,
        billing_tag=None,
        sandbox={"profileId": None},
    )
    await user.create_session(**options)
    state["etag"] = False
    with pytest.raises(OutcomeUnknownError):
        await user.create_session(**options)
    sent = {
        "agentTemplateId": "template_1",
        "additionalInstructions": None,
        "billingTag": None,
        "sandbox": {"profileId": None},
    }
    assert [call.json() for call in service.calls] == [sent, sent]


async def test_unusable_template_ids_fail_before_dispatch_in_creation_and_filters() -> None:
    organization, service = serve(lambda request, service: json_response({}))
    user = organization.user("user_1")
    period = {"from_": "2030-01-01T00:00:00.000Z", "to": "2030-01-02T00:00:00.000Z"}
    for template_id in ["", " t", "t ", "x" * 65, 1, ["t"]]:
        expected = ValueError if isinstance(template_id, str) else TypeError
        with pytest.raises(expected):
            await user.create_session(template_id=template_id, idempotency_key="ok")  # type: ignore[arg-type]
        with pytest.raises(expected):
            await user.list_sessions(template_id=template_id)  # type: ignore[arg-type]
        with pytest.raises(expected):
            await organization.get_user_usage("user_1", **period, template_id=template_id)  # type: ignore[arg-type]
        with pytest.raises(expected):
            await organization.get_user_usage_details("user_1", **period, template_id=template_id)  # type: ignore[arg-type]
    # None means an omitted filter in Python, but no Template at creation.
    with pytest.raises(TypeError):
        await user.create_session(template_id=None, idempotency_key="ok")  # type: ignore[arg-type]
    assert service.calls == []


async def test_session_options_with_unsupported_types_or_keys_fail_before_dispatch() -> None:
    user, service = user_for(lambda request, service: json_response({}))
    for options, expected in [
        ({"additional_instructions": 5}, TypeError),
        ({"additional_instructions": {"text": "x"}}, TypeError),
        ({"sandbox": None}, TypeError),
        ({"sandbox": "work"}, TypeError),
        ({"sandbox": {"cwd": "work"}}, ValueError),
        ({"sandbox": {"profileId": 1}}, TypeError),
        ({"sandbox": {"workingDirectory": None}}, TypeError),
    ]:
        with pytest.raises(expected):
            await user.create_session(template_id="template_1", idempotency_key="ok", **options)
    assert service.calls == []


async def test_model_discovery_accepts_no_options_or_an_omitted_request_id() -> None:
    user, service = user_for(lambda request, service: json_response({"models": []}))
    await user.list_models()
    await user.list_models(request_id=None)
    assert [call.path for call in service.calls] == ["/api/v1/org/users/user_1/agent-models"] * 2


def test_the_packaged_workflow_passes_session_options_through() -> None:
    def handler(received):
        if received.method == "GET":
            return 200, {}, {"templates": []}
        return 201, {"etag": '"model-1"'}, {"session": {"sessionId": "session_1"}}

    async def run(base_url: str) -> None:
        result = await create_session_workflow(
            base_url=base_url,
            organization_api_key=KEY,
            user_id="user_1",
            template_id="template_1",
            session_creation_key="example-options",
            additional_instructions="Use metric units.",
            sandbox={"workingDirectory": "work"},
            acknowledge_session_recall=True,
        )
        await result["organization"].close()

    with LocalServer(handler) as server:
        asyncio.run(run(server.base_url))
    assert [request.json() for request in server.requests if request.method == "POST"] == [
        {
            "agentTemplateId": "template_1",
            "additionalInstructions": "Use metric units.",
            "sandbox": {"workingDirectory": "work"},
            "acknowledgeSessionRecall": True,
        }
    ]


async def test_sessions_can_be_listed_by_their_current_template() -> None:
    user, service = user_for(
        lambda request, service: json_response({"sessions": [], "hasMore": False, "nextCursor": None})
    )
    await user.list_sessions(template_id="template/1", limit=5)
    await user.list_sessions()
    with pytest.raises(TypeError):
        await user.list_sessions(agent_id="agent_1")  # type: ignore[call-arg]
    assert [call.path for call in service.calls] == [
        "/api/v1/org/users/user_1/sessions?limit=5&agentTemplateId=template%2F1",
        "/api/v1/org/users/user_1/sessions",
    ]


async def test_template_discovery_preserves_empty_continuation_pages_and_model_discovery_accepts_dynamic_ids() -> None:
    def handler(request, service):
        if service.calls[-1].path.startswith("/api/v1/org/users/user_1/agent-models"):
            return json_response(
                {
                    "models": [{"model": "future.model-1", "label": "Future", "availability": "available"}],
                    "effectiveDefaultModel": "future.model-1",
                }
            )
        return json_response({"templates": [], "hasMore": True, "nextCursor": "opaque+/="})

    user, service = user_for(handler)
    assert await user.list_agent_templates(limit=1, cursor="opaque+/=") == {
        "templates": [],
        "hasMore": True,
        "nextCursor": "opaque+/=",
    }
    assert (await user.list_models())["models"][0]["model"] == "future.model-1"
    # Models no longer depend on an Agent.
    with pytest.raises(TypeError):
        await user.list_models(agent_id="agent_1")  # type: ignore[call-arg]
    assert [call.path for call in service.calls] == [
        "/api/v1/org/users/user_1/agent-templates?limit=1&cursor=opaque%2B%2F%3D",
        "/api/v1/org/users/user_1/agent-models",
    ]


async def test_retired_creation_selectors_missing_identities_and_unusable_owners_fail_before_dispatch() -> None:
    organization, service = serve(lambda request, service: json_response({}))
    user = organization.user("user_1")
    for options, expected in [
        ({"idempotency_key": "ok"}, TypeError),
        ({"template_id": " ", "idempotency_key": "ok"}, ValueError),
        ({"template_id": "t"}, TypeError),
        ({"template_id": "t", "idempotency_key": "bad key"}, ValueError),
        # Retired selectors: the Agent and the Template's wire name.
        ({"agent_id": "agent_1", "idempotency_key": "ok"}, TypeError),
        ({"template_id": "t", "agent_id": "agent_1", "idempotency_key": "ok"}, TypeError),
        ({"agent_template_id": "t", "idempotency_key": "ok"}, TypeError),
        # Native Hardware binding is not part of the SDK.
        ({"template_id": "t", "idempotency_key": "ok", "hardware_ids": ["hw_1"]}, TypeError),
        ({"template_id": "t", "idempotency_key": "ok", "template_version": 1}, TypeError),
    ]:
        with pytest.raises(expected):
            await user.create_session(**options)
    for name in ("create_agent", "list_agents", "get_agent"):
        assert not hasattr(user, name)
    for user_id, expected in [
        (None, TypeError),
        ("", ValueError),
        ("User 1", ValueError),
        ("a/b", ValueError),
        ("x" * 65, ValueError),
    ]:
        with pytest.raises(expected):
            organization.user(user_id)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        OrganizationClient(base_url="http://tidblink.test", api_key=KEY, user_id="other")  # type: ignore[call-arg]
    assert service.calls == []
    assert KEY not in repr(user)


async def test_stopping_creation_after_dispatch_does_not_replay_it() -> None:
    # TypeScript aborts with a signal and gets OutcomeUnknownError; in Python the
    # caller cancels the task, and the cancellation propagates unchanged.
    user, service = user_for(lambda request, service: error_response(503, "unavailable", "Unknown"))
    task = asyncio.create_task(user.create_session(template_id="template_1", idempotency_key="abort-recover"))
    while not service.calls:
        await asyncio.sleep(0.005)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.3)
    assert len(service.calls) == 1


class _Truncated(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'{"error":'
        raise httpx.ReadError("Connection reset while reading the body")


async def test_a_truncated_retryable_response_still_recovers_creation_with_the_original_identity() -> None:
    def handler(request, service):
        if len(service.calls) == 1:
            return httpx.Response(
                503,
                headers={"content-type": "application/json", "content-length": "1000"},
                stream=_Truncated(),
            )
        return created()

    user, service = user_for(handler)
    session = await user.create_session(template_id="template_1", idempotency_key="recover-truncated")
    assert session.id == "session_1"
    assert [call.headers["idempotency-key"] for call in service.calls] == ["recover-truncated"] * 2


def test_the_packaged_workflow_keeps_original_creation_inputs_when_catalog_defaults_change() -> None:
    catalog_reads = {"count": 0}

    def handler(received):
        if received.path.startswith("/api/v1/org/users/user_1/agent-templates"):
            return 200, {}, {
                "templates": [{"agentTemplateId": "changed-template"}],
                "hasMore": False,
                "nextCursor": None,
            }
        if received.path.startswith("/api/v1/org/users/user_1/agent-models"):
            catalog_reads["count"] += 1
            return 200, {}, {
                "effectiveDefaultModel": "first-default" if catalog_reads["count"] == 1 else "new-default"
            }
        return 201, {"etag": '"model-1"'}, {"session": {"sessionId": "session_1", "model": "first-default"}}

    async def run(base_url: str) -> tuple[str, str]:
        options = dict(
            base_url=base_url,
            organization_api_key=KEY,
            user_id="user_1",
            template_id="template_1",
            session_creation_key="example-session",
        )
        first = await create_session_workflow(**options)
        second = await create_session_workflow(**options)
        await first["organization"].close()
        await second["organization"].close()
        return first["session"].id, second["session"].id

    with LocalServer(handler) as server:
        first_id, second_id = asyncio.run(run(server.base_url))
    assert first_id == second_id
    creations = [(request.path, request.json()) for request in server.requests if request.method == "POST"]
    creation = ("/api/v1/org/users/user_1/sessions", {"agentTemplateId": "template_1"})
    assert creations == [creation, creation]
