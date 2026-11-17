"""Ported from typescript/test/client-tools.test.mjs."""

from __future__ import annotations

import pytest

from core_support import session_for, user_for
from support import json_response
from tidblink_server import OutcomeUnknownError

TOOL = {
    "type": "function",
    "name": "lookup_order",
    "description": "Read an order status",
    "parameters": {
        "type": "object",
        "properties": {"orderId": {"type": "string"}},
        "required": ["orderId"],
        "additionalProperties": False,
    },
}


async def test_declarations_are_sent_at_creation_and_read_or_replaced_with_their_own_etag() -> None:
    def handler(request, service):
        if service.calls[-1].path == "/api/v1/org/users/user_1/sessions":
            return json_response({"session": {"sessionId": "session_1"}}, 201, {"etag": '"model.session_1.0"'})
        replacing = request.method == "PUT"
        return json_response(
            {"revision": 2 if replacing else 1, "tools": [] if replacing else [TOOL]},
            headers={"etag": '"tools.session_1.2"' if replacing else '"tools.session_1.1"'},
        )

    user, service = user_for(handler)
    session = await user.create_session(template_id="template_1", idempotency_key="create-1", tools=[TOOL])
    current = await session.get_client_tools()
    assert current == {"revision": 1, "tools": [TOOL], "etag": '"tools.session_1.1"'}
    replaced = await session.replace_client_tools([], if_match=current["etag"])
    assert replaced == {"revision": 2, "tools": [], "etag": '"tools.session_1.2"'}
    assert [
        (call.method, call.path, call.json() if call.body else None, call.headers.get("if-match"))
        for call in service.calls
    ] == [
        ("POST", "/api/v1/org/users/user_1/sessions", {"agentTemplateId": "template_1", "tools": [TOOL]}, None),
        ("GET", "/api/v1/org/users/user_1/sessions/session_1/tools", None, None),
        ("PUT", "/api/v1/org/users/user_1/sessions/session_1/tools", {"tools": []}, '"tools.session_1.1"'),
    ]


async def test_replacement_needs_an_explicit_revision_and_an_unusable_success_is_uncertain() -> None:
    session, service = session_for(lambda request, service: json_response({"revision": 2, "tools": []}))
    with pytest.raises(ValueError):
        await session.replace_client_tools([], if_match="")
    assert service.calls == []
    with pytest.raises(OutcomeUnknownError) as caught:
        await session.replace_client_tools([], if_match='"tools.session_1.1"')
    assert caught.value.session_id == "session_1"
    assert [call.method for call in service.calls] == ["PUT"]


async def test_read_options_cannot_turn_a_read_into_an_unsafe_replacement() -> None:
    session, service = session_for(
        lambda request, service: json_response(
            {"revision": 2, "tools": [], "calls": []}, headers={"etag": '"tools.session_1.2"'}
        )
    )
    with pytest.raises(TypeError):
        await session.get_client_tools(  # type: ignore[call-arg]
            method="PUT", body={"tools": []}, if_match='"tools.session_1.1"'
        )
    assert service.calls == []
