"""Ported from typescript/test/gated-agents.test.mjs."""

from __future__ import annotations

import ast
from pathlib import Path

import httpx
import pytest

import tidblink_server
from core_support import disconnect, user_for
from support import error_response, json_response
from tidblink_server import ConflictError, OutcomeUnknownError, Session, TiDBLinkApiError
from tidblink_server import gated
from tidblink_server.gated import Agents, FeatureNotEnabledError, agents

WIRE_AGENT = {
    "agentId": "agent_1",
    "userId": "user_1",
    "name": "Support",
    "sessionDefaults": {
        "agentTemplateId": "template_1",
        "additionalInstructions": "Use metric units.",
        "sandbox": {"profileId": None, "workingDirectory": "agents/support"},
    },
    "createdAt": "2026-11-20T00:00:00.000Z",
    "updatedAt": "2026-11-20T00:00:00.000Z",
}
SDK_DEFAULTS = {
    "template_id": "template_1",
    "additional_instructions": "Use metric units.",
    "sandbox": {"profileId": None, "workingDirectory": "agents/support"},
}
SDK_AGENT = {**WIRE_AGENT, "sessionDefaults": SDK_DEFAULTS}
AGENTS = "/api/v1/org/users/user_1/agents"


def responses(*items):
    """Answer requests in order."""
    queue = list(items)

    def handler(request, service):
        item = queue.pop(0)
        return item() if callable(item) else item

    return handler


def not_enabled():
    return json_response(
        {
            "error": {
                "code": "agent_preview_not_enabled",
                "message": "The Agent preview is not enabled for this Organization.",
            }
        },
        403,
        {"x-request-id": "req_refused"},
    )


async def test_agents_sends_no_request_and_accepts_only_an_end_user_client() -> None:
    user, service = user_for(responses())
    assert isinstance(agents(user), Agents)
    assert service.calls == []
    for value in (None, {}, "user_1"):
        with pytest.raises(TypeError):
            agents(value)  # type: ignore[arg-type]


async def test_create_sends_template_id_as_agent_template_id_and_returns_the_agent_with_its_etag() -> None:
    user, service = user_for(
        responses(disconnect, json_response({"agent": WIRE_AGENT}, 201, {"etag": '"agent_1.r1"'}))
    )
    agent = await agents(user).create(
        name="Support", session_defaults=SDK_DEFAULTS, idempotency_key="agent-create-1"
    )
    assert agent == {**SDK_AGENT, "etag": '"agent_1.r1"'}
    assert len(service.calls) == 2
    for call in service.calls:
        assert call.method == "POST"
        assert call.path == AGENTS
        assert call.headers["idempotency-key"] == "agent-create-1"
        assert call.json() == {"name": "Support", "sessionDefaults": WIRE_AGENT["sessionDefaults"]}
    assert service.calls[0].headers["x-request-id"] == service.calls[1].headers["x-request-id"]


@pytest.mark.parametrize(
    "changes",
    [
        {"idempotency_key": "bad key"},
        {"session_defaults": {}},
        {"session_defaults": {"template_id": " template_1"}},
        {"session_defaults": {"template_id": "template_1", "tools": []}},
        {"session_defaults": {"agentTemplateId": "template_1"}},
        {"name": " "},
    ],
)
async def test_create_checks_the_template_creation_key_and_names_locally(changes) -> None:
    user, service = user_for(responses())
    arguments = {
        "name": "Support",
        "session_defaults": {"template_id": "template_1"},
        "idempotency_key": "agent-create-1",
        **changes,
    }
    with pytest.raises((TypeError, ValueError)):
        await agents(user).create(**arguments)
    assert service.calls == []


async def test_an_uncertain_creation_is_an_unknown_outcome() -> None:
    user, _ = user_for(responses(json_response({"agent": WIRE_AGENT}, 201)))
    with pytest.raises(OutcomeUnknownError):
        await agents(user).create(name="Support", session_defaults=SDK_DEFAULTS, idempotency_key="agent-create-1")


async def test_list_pages_agents_without_etags() -> None:
    user, service = user_for(
        responses(json_response({"agents": [WIRE_AGENT], "hasMore": True, "nextCursor": "cursor_2"}))
    )
    page = await agents(user).list(limit=1, cursor="cursor_1")
    assert page == {"agents": [SDK_AGENT], "hasMore": True, "nextCursor": "cursor_2"}
    assert service.calls[0].path == f"{AGENTS}?limit=1&cursor=cursor_1"


async def test_get_returns_the_agent_with_the_etag_update_needs() -> None:
    user, service = user_for(
        responses(
            json_response({"agent": WIRE_AGENT}, 200, {"etag": '"agent/1.r3"'}),
            json_response({"agent": WIRE_AGENT}),
        )
    )
    agent = await agents(user).get("agent/1")
    assert agent["etag"] == '"agent/1.r3"'
    assert agent["sessionDefaults"] == SDK_DEFAULTS
    assert service.calls[0].url.raw_path.decode() == f"{AGENTS}/agent%2F1"
    with pytest.raises(tidblink_server.TiDBLinkError, match="ETag"):
        await agents(user).get("agent_1")
    with pytest.raises(ValueError):
        await agents(user).get("")


async def test_update_sends_if_match_and_only_the_given_fields() -> None:
    updated = {**WIRE_AGENT, "name": "Help"}
    user, service = user_for(
        responses(
            json_response({"agent": updated}, 200, {"etag": '"agent_1.r2"'}),
            json_response({"agent": updated}, 200, {"etag": '"agent_1.r3"'}),
            error_response(412, "precondition_failed", "If-Match is stale"),
        )
    )
    agent = await agents(user).update("agent_1", name="Help", if_match='"agent_1.r1"')
    assert agent["name"] == "Help" and agent["etag"] == '"agent_1.r2"'
    call = service.calls[0]
    assert (call.method, call.path, call.headers["if-match"]) == ("PATCH", f"{AGENTS}/agent_1", '"agent_1.r1"')
    assert call.json() == {"name": "Help"}
    await agents(user).update(
        "agent_1", session_defaults={"template_id": "template_2", "model": "model_1"}, if_match='"agent_1.r2"'
    )
    assert service.calls[1].json() == {"sessionDefaults": {"agentTemplateId": "template_2", "model": "model_1"}}
    with pytest.raises(ConflictError):
        await agents(user).update("agent_1", name="Again", if_match='"agent_1.r1"')
    assert len(service.calls) == 3
    with pytest.raises(ValueError):
        await agents(user).update("agent_1", name="Help", if_match="")
    with pytest.raises(TypeError):
        await agents(user).update("agent_1", session_defaults={"model": "model_1"}, if_match='"x"')  # type: ignore[typeddict-item]
    assert len(service.calls) == 3


async def test_delete_removes_the_agent_and_reports_a_lost_answer_as_unknown() -> None:
    user, service = user_for(responses(httpx.Response(204), disconnect))
    assert await agents(user).delete("agent_1") is None
    assert (service.calls[0].method, service.calls[0].path) == ("DELETE", f"{AGENTS}/agent_1")
    with pytest.raises(OutcomeUnknownError):
        await agents(user).delete("agent_1")
    assert len(service.calls) == 2


async def test_create_session_returns_an_ordinary_session_of_the_end_user() -> None:
    record = {"sessionId": "session_1", "model": "model_1"}
    user, service = user_for(
        responses(
            disconnect,
            json_response({"agentId": "agent_1", "session": record}, 201, {"etag": '"model-1"'}),
            json_response({"session": record}, 200, {"etag": '"model-1"'}),
        )
    )
    tools = [{"type": "function", "name": "lookup_order", "description": "Read an order", "parameters": {"type": "object"}}]
    session = await agents(user).create_session(
        "agent_1",
        idempotency_key="agent-session-1",
        model="model_1",
        billing_tag={"key": "agent", "value": "support"},
        tools=tools,
    )
    assert isinstance(session, Session)
    assert session.id == "session_1" and session.data == record
    for call in service.calls[:2]:
        assert (call.method, call.path) == ("POST", f"{AGENTS}/agent_1/sessions")
        assert call.headers["idempotency-key"] == "agent-session-1"
        assert call.json() == {"model": "model_1", "billingTag": {"key": "agent", "value": "support"}, "tools": tools}
    await session.refresh()
    assert service.calls[2].path == "/api/v1/org/users/user_1/sessions/session_1"


async def test_create_session_reports_lost_receipts_as_unknown() -> None:
    user, service = user_for(
        responses(
            json_response({"agentId": "agent_1", "session": {"sessionId": "session_1"}}, 201),
            json_response({"agentId": "agent_1"}, 201, {"etag": '"model-1"'}),
        )
    )
    with pytest.raises(ValueError):
        await agents(user).create_session("agent_1", idempotency_key="bad key")
    assert service.calls == []
    for _ in range(2):
        with pytest.raises(OutcomeUnknownError):
            await agents(user).create_session("agent_1", idempotency_key="agent-session-1")


async def test_list_sessions_returns_the_ordinary_session_page() -> None:
    user, service = user_for(
        responses(
            json_response(
                {"sessions": [{"sessionId": "session_2"}, {"sessionId": "session_1"}], "hasMore": False, "nextCursor": None}
            )
        )
    )
    page = await agents(user).list_sessions("agent_1", limit=2)
    assert [session.id for session in page.sessions] == ["session_2", "session_1"]
    assert page.has_more is False and page.next_cursor is None
    assert service.calls[0].path == f"{AGENTS}/agent_1/sessions?limit=2"


async def test_an_organization_not_enabled_gets_feature_not_enabled_error_from_every_method() -> None:
    user, service = user_for(responses(*[not_enabled] * 7))
    each = agents(user)
    calls = [
        lambda: each.create(name="Support", session_defaults=SDK_DEFAULTS, idempotency_key="agent-create-1"),
        lambda: each.list(),
        lambda: each.get("agent_1"),
        lambda: each.update("agent_1", name="Help", if_match='"agent_1.r1"'),
        lambda: each.delete("agent_1"),
        lambda: each.create_session("agent_1", idempotency_key="agent-session-1"),
        lambda: each.list_sessions("agent_1"),
    ]
    for invoke in calls:
        with pytest.raises(FeatureNotEnabledError) as raised:
            await invoke()
        error = raised.value
        assert isinstance(error, TiDBLinkApiError)
        assert (error.feature, error.status, error.code) == ("agents", 403, "agent_preview_not_enabled")
        assert error.request_id == "req_refused"
        assert "not enabled for this Organization" in error.message
        assert "preview" not in error.message.lower()
    assert len(service.calls) == len(calls)


async def test_other_refusals_keep_their_ordinary_errors() -> None:
    user, _ = user_for(
        responses(
            error_response(403, "auth_kind_forbidden"),
            error_response(404, "agent_not_found"),
            error_response(409, "session_creation_conflict"),
        )
    )
    each = agents(user)
    for invoke, code, error_class in [
        (lambda: each.list(), "auth_kind_forbidden", TiDBLinkApiError),
        (lambda: each.get("agent_2"), "agent_not_found", TiDBLinkApiError),
        (lambda: each.create_session("agent_1", idempotency_key="agent-session-1"), "session_creation_conflict", ConflictError),
    ]:
        with pytest.raises(error_class) as raised:
            await invoke()
        assert not isinstance(raised.value, FeatureNotEnabledError)
        assert raised.value.code == code


def test_the_root_package_neither_exports_nor_imports_the_gated_package() -> None:
    assert sorted(gated.__all__) == sorted(
        ["FeatureNotEnabledError", "GatedFeature", "agents", "Agents", "Agent", "AgentRecord", "AgentPage", "AgentSessionDefaults"]
    )
    for name in gated.__all__:
        assert name not in tidblink_server.__all__
        assert not hasattr(tidblink_server, name)
    package = Path(tidblink_server.__file__).parent
    for source in package.glob("*.py"):
        for node in ast.walk(ast.parse(source.read_text())):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("gated"), source.name
                assert "gated" not in (node.module or "").split("."), source.name
