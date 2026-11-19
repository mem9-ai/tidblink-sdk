"""Port of ``typescript/test/connectors.test.mjs``: Connector reads and Connectors served over Backend Connections."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from backend_support import API_KEY, FakeCore, Harness, Peer, by_method, deadline_in, harness, until  # noqa: F401
from examples.serve_connector import MemoryOrders, serve_orders_connector
from support import json_response, serve
from tidblink_server import (
    BackendConnectionClosedError,
    BackendRequestError,
    ClientToolCallContext,
    ClientToolHandler,
    ServedConnector,
    ServedConnectorEnd,
    TiDBLinkError,
    ToolCallConnector,
)
from tidblink_server._backend_pool import BackendConnectionPool, SessionConnectionEnd
from tidblink_server.connector_serve import serve_connector

ROOT = Path(__file__).resolve().parents[1]
CONNECTOR_ID = "conn_00000000-0000-0000-0000-000000000001"
record = {
    "connectorId": CONNECTOR_ID,
    "displayName": "orders",
    "createdAt": "2026-11-19T00:00:00.000Z",
    "updatedAt": "2026-11-19T00:00:00.000Z",
}

#: How the stand-in core answers by default: it serves Connectors, claims calls and accepts results.
DEFAULT_ANSWERS: dict[str, dict[str, Any]] = {
    "connector.serve": {"result": {"connectorId": CONNECTOR_ID}},
    "connector.withdraw": {"result": {}},
    "tool.claim": {"result": {"claimed": True}},
    "tool.result": {"result": {"accepted": True}},
}


async def start(harness: Harness, respond: Any = None, **options: Any) -> FakeCore:
    def answer(message: dict[str, Any], peer: Peer, index: int) -> Any:
        custom = respond(message, peer, index) if respond is not None else None
        return custom if custom is not None else DEFAULT_ANSWERS.get(message.get("method", ""))

    return await harness.core(respond=answer, attach_result=lambda _: {"generation": 1, "cursor": "0"}, **options)


def pool_for(harness: Harness, core: FakeCore) -> BackendConnectionPool:
    pool = BackendConnectionPool(base_url=core.base_url, api_key=API_KEY, idle_close=0.05)
    harness.closers.append(pool.close)
    return pool


def call(call_id: str, name: str, args: dict[str, Any] | None = None, **overrides: Any) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "method": "tool.call",
        "params": {
            "callId": call_id,
            "sessionId": "sess_9",
            "userId": "user_9",
            "turnId": "turn_9",
            "name": name,
            "arguments": args or {},
            "claimBy": deadline_in(5),
            "deadlineAt": deadline_in(60),
            "connector": {"id": CONNECTOR_ID, "name": "orders"},
            **overrides,
        },
    }


def lookup(runs: list[tuple[dict[str, Any], ClientToolCallContext]] | None = None) -> dict[str, Any]:
    async def get_order(args: dict[str, Any], context: ClientToolCallContext) -> str:
        if runs is not None:
            runs.append((args, context))
        return f"Order {args['orderId']} shipped"

    return {
        "get_order": ClientToolHandler(
            description="Read one of the user's orders",
            parameters={"type": "object", "properties": {"orderId": {"type": "string"}}, "required": ["orderId"]},
            run=get_order,
        )
    }


def results_of(peer: Peer) -> dict[str, dict[str, Any]]:
    return {message["params"]["callId"]: message["params"] for message in by_method(peer, "tool.result")}


async def test_connectors_are_read_and_registration_and_callbacks_are_gone() -> None:
    import tidblink_server

    def handler(request, service):
        if request.url.path.endswith("/connectors"):
            return json_response({"connectors": [record]})
        return json_response(record)

    organization, service = serve(handler)
    async with organization:
        assert await organization.connectors.list(request_id="list-1") == [record]
        assert await organization.connectors.get(CONNECTOR_ID) == record
        for method in ("register", "update", "activate", "deactivate"):
            assert not hasattr(organization.connectors, method), method
    assert [(c.method, c.pathname) for c in service.calls] == [
        ("GET", "/api/v1/org/connectors"),
        ("GET", f"/api/v1/org/connectors/{CONNECTOR_ID}"),
    ]
    assert service.calls[0].headers["x-request-id"] == "list-1"
    for name in (
        "ConnectorKeySet",
        "ConnectorAuthError",
        "verify_connector_callback",
        "read_connector_call_context",
        "CONNECTOR_CALLBACK_ISSUER",
        "CONNECTOR_CALLBACK_AUDIENCE",
        "CONNECTOR_CALL_CONTEXT_KEY",
    ):
        assert not hasattr(tidblink_server, name), name


async def test_serve_connector_checks_names_functions_and_parameters_before_connecting(harness: Harness) -> None:
    core = await start(harness)
    pool = pool_for(harness, core)

    def run(args: dict[str, Any], context: ClientToolCallContext) -> str:
        return ""

    def tool(parameters: Any = None, **overrides: Any) -> dict[str, Any]:
        return {
            "description": "A function",
            "parameters": {"type": "object", "properties": {}} if parameters is None else parameters,
            "run": run,
            **overrides,
        }

    cases: list[tuple[dict[str, Any], type[Exception], str]] = [
        ({"name": "Orders", "tools": {}}, ValueError, "uppercase name"),
        ({"name": "orders_", "tools": {}}, ValueError, "trailing underscore"),
        ({"name": "my__orders", "tools": {}}, ValueError, "double underscore"),
        ({"name": "o" * 30, "tools": {}}, ValueError, "30 characters"),
        ({"name": "", "tools": {}}, ValueError, "empty name"),
        ({"name": "orders\n", "tools": {}}, ValueError, "trailing newline"),
        ({"name": 7, "tools": {}}, TypeError, "name not a string"),
        ({"name": "orders", "tools": {"get-order": tool()}}, ValueError, "function name with a dash"),
        ({"name": "orders", "tools": {"get_order": tool(run=None)}}, TypeError, "no run"),
        ({"name": "orders", "tools": {"get_order": tool({"properties": {}})}}, ValueError, "parameters without type object"),
        (
            {"name": "orders", "tools": {"get_order": tool({"type": "object", "properties": {"async": {"type": "boolean"}}})}},
            ValueError,
            "defines async",
        ),
        ({"name": "orders", "tools": {"get_order": tool({"type": "object", "required": ["async"]})}}, ValueError, "requires async"),
        ({"name": "orders", "tools": {"get_order": tool(description="")}}, ValueError, "empty description"),
        ({"name": "orders", "tools": {"get_order": tool(description="d" * 4097)}}, ValueError, "long description"),
        ({"name": "orders", "tools": {f"f{i}": tool() for i in range(65)}}, ValueError, "65 functions"),
        ({"name": "orders", "tools": []}, TypeError, "tools list"),
        ({"name": "orders", "tools": {"get_order": tool(extra=True)}}, ValueError, "unknown handler field"),
        ({"name": "orders", "tools": {}, "on_error": "log"}, TypeError, "on_error not callable"),
        ({"name": "orders", "tools": {}, "extra": True}, TypeError, "unknown option"),
    ]
    for options, error, label in cases:
        with pytest.raises(error):
            await serve_connector(pool, **options)
            pytest.fail(label)
    assert core.peers == []
    # The longest names and descriptions are accepted.
    served = await serve_connector(pool, name="o" * 29, tools={"f" * 29: tool(description="d" * 4096)})
    assert served.connector_id == CONNECTOR_ID


async def test_a_served_connector_runs_a_call_from_any_session_on_the_connection_that_delivered_it(
    harness: Harness,
) -> None:
    core = await start(harness)
    organization = harness.client(core)
    runs: list[tuple[dict[str, Any], ClientToolCallContext]] = []
    served = await organization.serve_connector(name="orders", tools=lookup(runs))
    assert isinstance(served, ServedConnector)
    assert served.name == "orders"
    assert served.connector_id == CONNECTOR_ID
    peer = core.peers[0]
    assert by_method(peer, "connector.serve")[0]["params"] == {
        "name": "orders",
        "tools": [
            {
                "type": "function",
                "name": "get_order",
                "description": "Read one of the user's orders",
                "parameters": {"type": "object", "properties": {"orderId": {"type": "string"}}, "required": ["orderId"]},
            }
        ],
    }
    # No Session is attached: the call is routed by its Connector.
    peer.send(call("call_1", "get_order", {"orderId": "o_7"}))
    await until(lambda: len(by_method(peer, "tool.result")) == 1)
    assert by_method(peer, "tool.claim")[0]["params"] == {"callId": "call_1"}
    assert by_method(peer, "tool.result")[0]["params"] == {"callId": "call_1", "output": "Order o_7 shipped"}
    assert len(runs) == 1
    _, context = runs[0]
    assert isinstance(context.deadline_at, datetime)
    assert context == ClientToolCallContext(
        call_id="call_1",
        session_id="sess_9",
        user_id="user_9",
        turn_id="turn_9",
        name="get_order",
        deadline_at=context.deadline_at,
        connector=ToolCallConnector(id=CONNECTOR_ID, name="orders"),
    )
    # A call for a Connector this process does not serve is left unclaimed.
    peer.send(call("call_2", "get_order", {"orderId": "o_8"}, connector={"id": "conn_x", "name": "billing"}))
    await asyncio.sleep(0.05)
    assert len(by_method(peer, "tool.claim")) == 1


async def test_refused_claims_never_run_duplicates_run_once_and_unknown_functions_and_deadlines_become_errors(
    harness: Harness,
) -> None:
    def respond(message: dict[str, Any], peer: Peer, index: int) -> Any:
        call_id = message.get("params", {}).get("callId", "")
        if message.get("method") == "tool.claim" and call_id.startswith("refused"):
            return {"result": {"claimed": False, "reason": call_id[len("refused_") :]}}
        return None

    core = await start(harness, respond=respond)
    pool = pool_for(harness, core)
    runs: list[tuple[dict[str, Any], ClientToolCallContext]] = []

    async def slow(args: dict[str, Any], context: ClientToolCallContext) -> str:
        await asyncio.sleep(3600)
        return "never"

    await serve_connector(
        pool,
        name="orders",
        tools={
            **lookup(runs),
            "slow": {"description": "Never finishes", "parameters": {"type": "object"}, "run": slow},
        },
    )
    peer = core.peers[0]
    for reason in ("call_not_found", "call_settled", "claim_window_closed", "superseded"):
        peer.send(call(f"refused_{reason}", "get_order", {"orderId": "o_1"}))
    peer.send(call("call_1", "get_order", {"orderId": "o_1"}))
    peer.send(call("call_1", "get_order", {"orderId": "o_1"}))
    peer.send(call("call_2", "list_everything"))
    peer.send(call("call_3", "slow", deadlineAt=deadline_in(0.1)))
    await until(lambda: len(by_method(peer, "tool.result")) == 3)
    await asyncio.sleep(0.05)
    assert len(runs) == 1
    assert len(by_method(peer, "tool.claim")) == 7
    results = results_of(peer)
    assert sorted(results) == ["call_1", "call_2", "call_3"]
    assert results["call_2"]["error"] == {
        "message": "Connector orders has no function list_everything in this Application Server"
    }
    assert results["call_3"]["error"] == {"message": "The tool did not finish before its deadline"}


class Attachment:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id

    def attach_params(self) -> dict[str, Any]:
        return {"userId": "user_1", "sessionId": self.session_id}

    def attached(self, result: dict[str, Any], link: Any) -> None:
        pass

    def notify(self, method: str, params: dict[str, Any], link: Any, size: int) -> None:
        pass

    def ended(self, end: SessionConnectionEnd) -> None:
        pass


async def test_a_connector_is_declared_on_every_connection_including_ones_opened_later(harness: Harness) -> None:
    core = await start(harness, hello_params={"maxSessions": 1})
    pool = pool_for(harness, core)
    await pool.attach(Attachment("s1"))
    await pool.attach(Attachment("s2"))
    assert pool.connections == 2
    await serve_connector(pool, name="orders", tools=lookup())
    await until(lambda: all(len(by_method(peer, "connector.serve")) == 1 for peer in core.peers))
    await pool.attach(Attachment("s3"))
    assert pool.connections == 3
    await until(lambda: len(by_method(core.peers[2], "connector.serve")) == 1)
    # A call delivered on the second connection is claimed there.
    core.peers[1].send(call("call_1", "get_order", {"orderId": "o_1"}))
    await until(lambda: len(by_method(core.peers[1], "tool.claim")) == 1)
    assert by_method(core.peers[0], "tool.claim") == []


async def test_serving_keeps_one_connection_open_and_close_withdraws_it_and_idle_connections_close_again(
    harness: Harness,
) -> None:
    core = await start(harness)
    pool = pool_for(harness, core)
    served = await serve_connector(pool, name="orders", tools=lookup())
    await asyncio.sleep(0.2)
    assert pool.connections == 1
    assert len(core.peers) == 1
    await served.close()
    assert by_method(core.peers[0], "connector.withdraw")[0]["params"] == {"name": "orders"}
    assert await served.closed == ServedConnectorEnd("closed")
    assert (await core.peers[0].closed)[0] == 1000
    assert pool.connections == 0
    # Closing again does nothing, and the name is free again.
    await served.close()
    await serve_connector(pool, name="orders", tools=lookup())


async def test_a_lost_or_draining_connection_declares_the_connector_again_on_a_new_one(harness: Harness) -> None:
    core = await start(harness)
    pool = pool_for(harness, core)
    served = await serve_connector(pool, name="orders", tools=lookup())
    core.peers[0].destroy()
    second = await core.connection(1)
    await until(lambda: len(by_method(second, "connector.serve")) == 1)
    second.send({"jsonrpc": "2.0", "method": "connection.drain", "params": {"closeAfterMs": 5000}})
    third = await core.connection(2)
    await until(lambda: len(by_method(third, "connector.serve")) == 1)
    assert (await second.closed)[0] == 1000
    assert pool.connections == 1
    third.send(call("call_1", "get_order", {"orderId": "o_1"}))
    await until(lambda: len(by_method(third, "tool.result")) == 1)
    await asyncio.sleep(0.02)
    assert not served.closed.done()


async def test_a_duplicate_name_a_refused_declaration_and_a_revoked_key_end_serving(harness: Harness) -> None:
    revoked = False

    def on_connection(peer: Peer) -> None:
        if revoked:
            peer.close(4001, "authentication failed")

    def respond(message: dict[str, Any], peer: Peer, index: int) -> Any:
        if message.get("method") == "connector.serve" and message["params"]["name"] == "invalid":
            return {"error": {"code": -32602, "message": "Invalid params"}}
        return None

    core = await start(harness, respond=respond, on_connection=on_connection)
    pool = pool_for(harness, core)
    errors: list[BaseException] = []
    served = await serve_connector(pool, name="orders", tools=lookup(), on_error=errors.append)
    with pytest.raises(TiDBLinkError, match="already served"):
        await serve_connector(pool, name="orders", tools=lookup())
    with pytest.raises(BackendRequestError) as refused:
        await serve_connector(pool, name="invalid", tools=lookup())
    assert refused.value.code == -32602
    # The refused name is not served: it can be tried again.
    with pytest.raises(BackendRequestError):
        await serve_connector(pool, name="invalid", tools=lookup())
    revoked = True
    core.peers[0].close(4001, "revoked")
    end = await asyncio.wait_for(served.closed, 5)
    assert end.reason == "error"
    assert isinstance(end.error, BackendConnectionClosedError)
    assert end.error.code == 4001
    assert errors == []


async def test_closing_the_organization_client_ends_serving_and_refuses_new_connectors(harness: Harness) -> None:
    core = await start(harness)
    organization = harness.client(core)
    served = await organization.serve_connector(name="orders", tools=lookup())
    await organization.close()
    assert await served.closed == ServedConnectorEnd("closed")
    with pytest.raises(TiDBLinkError, match="closed"):
        await organization.serve_connector(name="orders", tools=lookup())


async def test_the_example_answers_with_the_calling_users_orders_only(harness: Harness) -> None:
    core = await start(harness)
    organization = harness.client(core)
    orders = MemoryOrders(
        {
            "user_9": [{"orderId": "o_1", "status": "shipped"}],
            "user_8": [{"orderId": "o_2", "status": "open"}],
        }
    )
    served = await serve_orders_connector(organization=organization, orders=orders)
    assert served.connector_id == CONNECTOR_ID
    peer = core.peers[0]
    assert [tool["name"] for tool in by_method(peer, "connector.serve")[0]["params"]["tools"]] == [
        "list_orders",
        "get_order",
    ]
    peer.send(call("call_1", "list_orders"))
    peer.send(call("call_2", "get_order", {"orderId": "o_2"}))
    await until(lambda: len(by_method(peer, "tool.result")) == 2)
    results = results_of(peer)
    assert json.loads(results["call_1"]["output"]) == [{"orderId": "o_1", "status": "shipped"}]
    assert results["call_2"]["error"] == {"message": "Order o_2 was not found"}


async def test_a_cancelled_serve_withdraws_its_declaration_and_frees_the_name(harness: Harness) -> None:
    def respond(message: dict[str, Any], peer: Peer, index: int) -> Any:
        if message.get("method") == "connector.serve" and len(by_method(peer, "connector.serve")) == 1:
            return "silent"
        return None

    core = await start(harness, respond=respond)
    pool = pool_for(harness, core)
    serving = asyncio.ensure_future(serve_connector(pool, name="orders", tools=lookup()))
    await until(lambda: core.peers and by_method(core.peers[0], "connector.serve"))
    serving.cancel()
    with pytest.raises(asyncio.CancelledError):
        await serving
    await until(lambda: by_method(core.peers[0], "connector.withdraw"))
    assert by_method(core.peers[0], "connector.withdraw")[0]["params"] == {"name": "orders"}
    served = await serve_connector(pool, name="orders", tools=lookup())
    assert served.connector_id == CONNECTOR_ID


def _script_env(**env: str) -> dict[str, str]:
    names = ("AGENT_SERVICE_URL", "ORGANIZATION_API_KEY", "CONNECTOR_NAME")
    clean = {key: value for key, value in os.environ.items() if key not in names}
    return {**clean, "PYTHONPATH": str(ROOT / "src"), **env}


async def test_the_example_script_serves_until_it_is_stopped(harness: Harness) -> None:
    core = await start(harness)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(ROOT / "examples" / "serve_connector.py"),
        env=_script_env(AGENT_SERVICE_URL=core.base_url, ORGANIZATION_API_KEY=API_KEY, CONNECTOR_NAME="shop_orders"),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        line = await asyncio.wait_for(process.stdout.readline(), 30)  # type: ignore[union-attr]
        assert json.loads(line) == {"connectorId": CONNECTOR_ID}
        assert by_method(core.peers[0], "connector.serve")[0]["params"]["name"] == "shop_orders"
        process.send_signal(signal.SIGTERM)
        assert await asyncio.wait_for(process.wait(), 30) == 0
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    assert (await core.peers[0].closed)[0] == 1000


async def test_the_example_script_reports_a_failure_to_serve() -> None:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(ROOT / "examples" / "serve_connector.py"),
        env=_script_env(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), 30)
    assert process.returncode == 1
    assert stdout == b""
    assert b"Organization API Key" in stderr


async def test_a_transient_refusal_of_a_further_declaration_is_retried_on_the_same_connection(harness: Harness) -> None:
    refusals = 1

    def respond(message: dict[str, Any], peer: Peer, index: int) -> Any:
        nonlocal refusals
        if message.get("method") == "connector.serve" and index == 1 and refusals > 0:
            refusals -= 1
            return {"error": {"code": -32603, "message": "Internal error"}}
        return None

    core = await start(harness, respond=respond, hello_params={"maxSessions": 1})
    pool = pool_for(harness, core)
    errors: list[BaseException] = []
    await serve_connector(pool, name="orders", tools=lookup(), on_error=errors.append)
    await pool.attach(Attachment("s1"))
    await pool.attach(Attachment("s2"))
    await until(lambda: len(core.peers) > 1 and len(by_method(core.peers[1], "connector.serve")) == 2)
    assert errors == []
