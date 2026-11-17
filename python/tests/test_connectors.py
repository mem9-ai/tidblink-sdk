"""Connectors and their platform callbacks (``connectors.test.mjs`` in the TypeScript SDK)."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from examples.http_connector import ConnectorTool, create_connector_handler, serve_connector
from support import Service, json_response, serve
from tidblink_server import ConflictError, TiDBLinkError
from tidblink_server.connector_callback import (
    ConnectorAuthError,
    ConnectorCallContext,
    ConnectorKeySet,
    read_connector_call_context,
    verify_connector_callback,
)

BASE_URL = "https://tidb-link.test"
ORGANIZATION_ID = "org_1"
CONNECTOR_ID = "conn_00000000-0000-0000-0000-000000000001"

signing = Ed25519PrivateKey.generate()


def encode(value: Any) -> str:
    raw = value if isinstance(value, bytes) else json.dumps(value).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


jwk = {
    "kty": "OKP",
    "crv": "Ed25519",
    "x": encode(signing.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)),
    "kid": "k1",
    "alg": "EdDSA",
    "use": "sig",
}


def token(claims: dict[str, Any] | None = None, header: dict[str, Any] | None = None, key: Ed25519PrivateKey = signing) -> str:
    now = int(time.time())
    signed = (
        f"{encode({'alg': 'EdDSA', 'typ': 'JWT', 'kid': 'k1', **(header or {})})}."
        + encode(
            {
                "iss": "tidb-link/connector-callback/v1",
                "aud": "tidb-link/connector",
                "organizationId": ORGANIZATION_ID,
                "connectorId": CONNECTOR_ID,
                "iat": now,
                "exp": now + 60,
                **(claims or {}),
            }
        )
    )
    return f"Bearer {signed}.{encode(key.sign(signed.encode()))}"


def key_set(answer: httpx.Response | None = None) -> tuple[ConnectorKeySet, list[str]]:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return answer if answer is not None else httpx.Response(200, json={"keys": [jwk]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return ConnectorKeySet(BASE_URL, http_client=client), requests


registration = {
    "connectorId": CONNECTOR_ID,
    "organizationId": ORGANIZATION_ID,
    "displayName": "Orders",
    "endpointUrl": "https://orders.example.com/mcp",
    "endpointVersion": 1,
    "transport": "streamable_http",
    "status": "inactive",
    "connectorVersion": 1,
    "createdAt": "2026-11-11T00:00:00.000Z",
    "updatedAt": "2026-11-11T00:00:00.000Z",
}


async def test_connectors_are_registered_edited_activated_and_deactivated_with_their_expected_version() -> None:
    def handler(request: httpx.Request, service: Service) -> httpx.Response:
        path = request.url.path
        if path.endswith("/connectors") and request.method == "GET":
            return json_response({"connectors": [registration]})
        if path.endswith("/deactivate"):
            return json_response({"error": {"code": "resource_conflict", "message": "Version changed"}}, 409)
        return json_response(registration, 201 if request.method == "POST" else 200)

    organization, service = serve(handler)
    async with organization:
        connectors = organization.connectors
        await connectors.register(display_name="Orders", endpoint_url=registration["endpointUrl"])
        assert (await connectors.list())[0]["connectorId"] == CONNECTOR_ID
        await connectors.get(CONNECTOR_ID)
        await connectors.update(CONNECTOR_ID, display_name="Orders v2", expected_connector_version=1)
        await connectors.activate(CONNECTOR_ID, expected_connector_version=2)
        with pytest.raises(ConflictError):
            await connectors.deactivate(CONNECTOR_ID, expected_connector_version=1)
        assert [
            f"{c.method} {c.pathname.removeprefix('/api/v1/org')} {json.dumps(c.json() if c.body else None, separators=(',', ':'))}"
            for c in service.calls
        ] == [
            'POST /connectors {"displayName":"Orders","endpointUrl":"https://orders.example.com/mcp"}',
            "GET /connectors null",
            f"GET /connectors/{CONNECTOR_ID} null",
            f'PATCH /connectors/{CONNECTOR_ID} {{"expectedConnectorVersion":1,"displayName":"Orders v2"}}',
            f'POST /connectors/{CONNECTOR_ID}/activate {{"expectedConnectorVersion":2}}',
            f'POST /connectors/{CONNECTOR_ID}/deactivate {{"expectedConnectorVersion":1}}',
        ]
        count = len(service.calls)
        with pytest.raises(TypeError):
            await connectors.activate(CONNECTOR_ID)  # type: ignore[call-arg]
        with pytest.raises(TypeError):
            await connectors.activate(CONNECTOR_ID, expected_connector_version=True)
        with pytest.raises(ValueError):
            await connectors.activate(CONNECTOR_ID, expected_connector_version=-1)
        with pytest.raises(ValueError):
            await connectors.register(display_name="")
        assert len(service.calls) == count


async def test_an_unusable_connector_receipt_is_an_uncertain_outcome() -> None:
    from tidblink_server import OutcomeUnknownError

    organization, _ = serve(lambda request, service: json_response({}, 201))
    async with organization:
        with pytest.raises(OutcomeUnknownError):
            await organization.connectors.register(display_name="Orders", endpoint_url=registration["endpointUrl"])


async def test_a_platform_callback_token_verifies_only_for_its_organization_connector_and_lifetime() -> None:
    keys, requests = key_set()
    options: dict[str, Any] = {"keys": keys, "organization_id": ORGANIZATION_ID, "connector_id": CONNECTOR_ID}
    claims = await verify_connector_callback(token(), **options)
    assert claims["connectorId"] == CONNECTOR_ID
    await verify_connector_callback(token(), **options)
    assert requests == [f"{BASE_URL}/api/v1/connectors/jwks"]

    now = int(time.time())
    other = Ed25519PrivateKey.generate()
    for label, authorization in [
        ("missing", None),
        ("not bearer", "Basic abc"),
        ("trailing newline", token() + "\n"),
        ("other Organization", token({"organizationId": "org_2"})),
        ("other Connector", token({"connectorId": "conn_other"})),
        ("issuer", token({"iss": "someone"})),
        ("audience", token({"aud": "someone"})),
        ("expired", token({"iat": now - 120, "exp": now - 60})),
        ("too long", token({"exp": now + 3600})),
        ("not yet valid", token({"iat": now + 60, "exp": now + 90})),
        ("fractional time", token({"exp": now + 30.5})),
        ("boolean time", token({"iat": True})),
        ("algorithm", token({}, {"alg": "HS256"})),
        ("signature", token({}, {}, other)),
    ]:
        with pytest.raises(ConnectorAuthError):
            await verify_connector_callback(authorization, **options)
            pytest.fail(label)

    # An unknown key does not refetch the keys again within the refresh interval.
    for _ in range(2):
        with pytest.raises(ConnectorAuthError):
            await verify_connector_callback(token({}, {"kid": "k2"}), **options)
    assert len(requests) == 1

    # A clock within the tolerance accepts the token; `now` overrides the clock.
    later = datetime.fromtimestamp(now + 63, timezone.utc)
    await verify_connector_callback(token({"iat": now, "exp": now + 60}), **options, now=later)
    with pytest.raises(ConnectorAuthError):
        await verify_connector_callback(token({"iat": now, "exp": now + 60}), **options, now=later, clock_tolerance=0)


async def test_the_key_set_shares_one_refresh_and_reports_unavailable_keys() -> None:
    keys, requests = key_set()
    await asyncio.gather(*(keys.refresh() for _ in range(3)))
    assert len(requests) == 1
    assert await keys.key("k1") is not None

    for answer, message in [
        (httpx.Response(503), "unavailable \\(status 503\\)"),
        (httpx.Response(302, headers={"location": "https://elsewhere.test/jwks"}), "unreachable"),
        (httpx.Response(200, json={"items": []}), "malformed"),
    ]:
        failing, _ = key_set(answer)
        with pytest.raises(TiDBLinkError, match=message) as caught:
            await verify_connector_callback(
                token(), keys=failing, organization_id=ORGANIZATION_ID, connector_id=CONNECTOR_ID
            )
        assert not isinstance(caught.value, ConnectorAuthError)

    assert ConnectorKeySet("https://tidb-link.test/prefix/").url == f"{BASE_URL}/api/v1/connectors/jwks"
    with pytest.raises(ValueError):
        ConnectorKeySet("ftp://tidb-link.test")


def test_the_call_context_is_read_from_tools_call_metadata_and_bound_to_the_organization() -> None:
    context = {
        "callId": "op_1",
        "sessionId": "session_1",
        "organizationId": ORGANIZATION_ID,
        "userId": "user_1",
        "deadline": "2099-01-01T00:00:00.000Z",
        "attempt": 1,
    }
    assert read_connector_call_context({"tidb-link/context": context}, organization_id=ORGANIZATION_ID) == (
        ConnectorCallContext(
            call_id="op_1",
            session_id="session_1",
            organization_id=ORGANIZATION_ID,
            user_id="user_1",
            deadline=datetime(2099, 1, 1, tzinfo=timezone.utc),
            attempt=1,
        )
    )
    for meta in [
        None,
        {},
        {"tidb-link/context": {**context, "organizationId": "org_2"}},
        {"tidb-link/context": {**context, "attempt": 2}},
        {"tidb-link/context": {**context, "attempt": True}},
        {"tidb-link/context": {**context, "userId": ""}},
        {"tidb-link/context": {**context, "deadline": "tomorrow"}},
    ]:
        with pytest.raises(ConnectorAuthError):
            read_connector_call_context(meta, organization_id=ORGANIZATION_ID)


@pytest.fixture
async def connector_url() -> AsyncIterator[tuple[str, list[str]]]:
    keys, _ = key_set()
    seen: list[str] = []

    async def lookup_order(arguments: dict[str, Any], context: ConnectorCallContext) -> str:
        seen.append(context.call_id)
        return f"order {arguments.get('orderId')} for {context.user_id}"

    handler = create_connector_handler(
        base_url=BASE_URL,
        organization_id=ORGANIZATION_ID,
        connector_id=CONNECTOR_ID,
        keys=keys,
        tools={
            "lookup_order": ConnectorTool(
                description="Look up an order",
                input_schema={"type": "object", "properties": {"orderId": {"type": "string"}}},
                run=lookup_order,
            )
        },
    )
    server = await serve_connector(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}/mcp", seen
    finally:
        server.close()
        await server.wait_closed()


async def test_the_example_http_connector_answers_mcp_requests_only_with_a_valid_platform_token(
    connector_url: tuple[str, list[str]],
) -> None:
    url, seen = connector_url
    async with httpx.AsyncClient() as http:

        async def rpc(body: Any, authorization: str | None = None) -> httpx.Response:
            return await http.post(
                url,
                headers={"authorization": authorization or token(), "content-type": "application/json"},
                content=json.dumps(body),
            )

        assert (await rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, "Bearer a.b.c")).status_code == 401
        init = (await rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})).json()
        assert init["result"]["capabilities"] == {"tools": {}}
        assert (await rpc({"jsonrpc": "2.0", "method": "notifications/initialized"})).status_code == 202
        listed = (await rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})).json()
        assert [tool["name"] for tool in listed["result"]["tools"]] == ["lookup_order"]
        call = (
            await rpc(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "lookup_order",
                        "arguments": {"orderId": "o1"},
                        "_meta": {
                            "tidb-link/context": {
                                "callId": "op_1",
                                "sessionId": "s1",
                                "organizationId": ORGANIZATION_ID,
                                "userId": "user_1",
                                "deadline": "2099-01-01T00:00:00.000Z",
                                "attempt": 1,
                            }
                        },
                    },
                }
            )
        ).json()
        assert call["result"]["content"] == [{"type": "text", "text": "order o1 for user_1"}]
        missing = (
            await rpc({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "lookup_order", "arguments": {}}})
        ).json()
        assert missing["error"]["code"] == -32602
        assert seen == ["op_1"]

        parse_error = await http.post(url, headers={"authorization": token()}, content=b"{")
        assert parse_error.status_code == 400
        assert parse_error.json()["error"]["code"] == -32700
        assert (await http.get(url, headers={"authorization": token()})).status_code == 405

        # A chunked request body is read too.
        async def chunks() -> AsyncIterator[bytes]:
            yield b'{"jsonrpc":"2.0",'
            yield b'"id":5,"method":"ping"}'

        ping = await http.post(url, headers={"authorization": token()}, content=chunks())
        assert ping.json() == {"jsonrpc": "2.0", "id": 5, "result": {}}
