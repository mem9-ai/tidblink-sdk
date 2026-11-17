"""Port of ``typescript/test/backend-transport.test.mjs``."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from backend_support import API_KEY, FakeCore, Harness, harness  # noqa: F401
from tidblink_server import _backend_connection
from tidblink_server._backend_connection import BackendTransport, backend_connection_url
from tidblink_server.errors import (
    BackendConnectionClosedError,
    BackendRequestError,
    TiDBLinkConnectionError,
    TiDBLinkError,
)

async def open_transport(core: FakeCore, harness: Harness, **options: Any) -> BackendTransport:
    transport = await BackendTransport.connect(
        url=core.url,
        api_key=API_KEY,
        on_notification=options.pop("on_notification", lambda *_: None),
        **options,
    )

    async def close() -> None:
        transport.close()
        await transport.wait_closed()

    harness.closers.append(close)
    return transport


def test_the_backend_connection_url_follows_the_base_urls_scheme_and_path() -> None:
    assert backend_connection_url("https://link.example.com") == "wss://link.example.com/api/v1/org/backend/ws"
    assert (
        backend_connection_url("http://127.0.0.1:3000/prefix/")
        == "ws://127.0.0.1:3000/prefix/api/v1/org/backend/ws"
    )


async def test_a_transport_authenticates_in_the_upgrade_headers_waits_for_hello_and_pairs_answers_by_id(
    harness: Harness,
) -> None:
    core = await harness.core(auto_answer=False)
    transport = await open_transport(core, harness)
    peer = await core.connection(0)
    assert peer.request.headers["authorization"] == f"Bearer {API_KEY}"
    assert peer.request.headers["sec-websocket-protocol"] == "tidb-link.backend.v1"
    assert API_KEY not in peer.request.path
    assert transport.hello.max_sessions == 1024
    assert transport.hello.claim_window_ms == 5000

    first = transport.request("session.attach", {"sessionId": "s1"})
    second = transport.request("session.attach", {"sessionId": "s2"})
    a = await peer.next()
    b = await peer.next()
    assert a == {"jsonrpc": "2.0", "id": a["id"], "method": "session.attach", "params": {"sessionId": "s1"}}
    assert a["id"] != b["id"]
    # Answers may arrive in any order.
    peer.send(
        {
            "jsonrpc": "2.0",
            "id": b["id"],
            "error": {"code": -32000, "message": "Refused", "data": {"reason": "connection_full"}},
        }
    )
    peer.send({"jsonrpc": "2.0", "id": a["id"], "result": {"generation": 3, "cursor": "9"}})
    assert await first == {"generation": 3, "cursor": "9"}
    with pytest.raises(BackendRequestError) as refused:
        await second
    assert isinstance(refused.value, TiDBLinkError)
    assert refused.value.code == -32000
    assert refused.value.reason == "connection_full"
    assert transport.pending_requests == 0
    transport.close()
    assert (await peer.closed)[0] == 1000
    assert (await transport.closed).code == 1000
    assert transport.open is False
    with pytest.raises(BackendConnectionClosedError):
        await transport.request("tool.claim", {"callId": "c"})


async def test_notifications_arrive_in_order_and_core_requests_are_answered_as_unknown_methods(
    harness: Harness,
) -> None:
    received: list[tuple[str, dict[str, Any]]] = []
    core = await harness.core(auto_answer=False)
    await open_transport(core, harness, on_notification=lambda method, params, size: received.append((method, params)))
    peer = await core.connection(0)
    peer.send({"jsonrpc": "2.0", "method": "session.event", "params": {"sessionId": "s", "cursor": "1"}})
    peer.send({"jsonrpc": "2.0", "method": "connection.drain", "params": {"closeAfterMs": 10}})
    peer.send({"jsonrpc": "2.0", "id": "core-1", "method": "future.request", "params": {}})
    assert await peer.next() == {
        "jsonrpc": "2.0",
        "id": "core-1",
        "error": {"code": -32601, "message": "Method not found"},
    }
    assert received == [
        ("session.event", {"sessionId": "s", "cursor": "1"}),
        ("connection.drain", {"closeAfterMs": 10}),
    ]


async def test_a_core_close_ends_the_transport_and_fails_requests_in_flight_4001_is_not_retryable(
    harness: Harness,
) -> None:
    core = await harness.core(auto_answer=False)
    transport = await open_transport(core, harness)
    peer = await core.connection(0)
    pending = transport.request("tool.result", {"callId": "c", "output": "ok"})
    await peer.next()
    peer.close(4001, "revoked")
    closed = await transport.closed
    assert isinstance(closed, BackendConnectionClosedError)
    assert isinstance(closed, TiDBLinkConnectionError)
    assert closed.code == 4001
    assert closed.reason == "revoked"
    assert closed.retryable is False
    with pytest.raises(BackendConnectionClosedError) as failed:
        await pending
    assert failed.value is closed


async def test_a_drained_or_restarted_replicas_close_is_retryable(harness: Harness) -> None:
    core = await harness.core(auto_answer=False)
    transport = await open_transport(core, harness)
    (await core.connection(0)).close(1012, "drained")
    closed = await transport.closed
    assert closed.code == 1012
    assert closed.retryable is True


async def test_opening_fails_with_the_close_code_when_the_core_refuses_the_key_before_hello(
    harness: Harness,
) -> None:
    core = await harness.core(
        send_hello=False, auto_answer=False, on_connection=lambda peer: peer.close(4001, "authentication failed")
    )
    with pytest.raises(BackendConnectionClosedError) as refused:
        await open_transport(core, harness)
    assert refused.value.code == 4001
    assert refused.value.retryable is False


async def test_opening_fails_when_the_upgrade_is_refused_or_the_first_message_is_not_hello(
    harness: Harness,
) -> None:
    refused = await harness.core(reject=lambda request: 503)
    with pytest.raises(BackendConnectionClosedError) as error:
        await open_transport(refused, harness)
    assert error.value.retryable is True
    garbled = await harness.core(
        send_hello=False,
        auto_answer=False,
        on_connection=lambda peer: peer.send({"jsonrpc": "2.0", "method": "hello", "params": {"connectionId": "x"}}),
    )
    with pytest.raises(BackendConnectionClosedError, match="hello"):
        await open_transport(garbled, harness)
    silent = await harness.core(send_hello=False, auto_answer=False)
    with pytest.raises(BackendConnectionClosedError, match="no hello"):
        await open_transport(silent, harness, open_timeout=0.05)


async def test_a_request_larger_than_the_announced_frame_limit_is_refused_before_sending(
    harness: Harness,
) -> None:
    core = await harness.core(hello_params={"maxFrameBytes": 64})
    transport = await open_transport(core, harness)
    with pytest.raises(TiDBLinkError, match="frame limit of 64 bytes"):
        await transport.request("tool.result", {"callId": "c", "output": "x" * 100})


async def test_a_silent_connection_is_probed_with_ping_and_closed_when_the_probe_goes_unanswered(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The TypeScript test waits whole seconds; shrink the interval bounds instead.
    monkeypatch.setattr(_backend_connection, "MIN_PROBE_INTERVAL", 0.05)
    core = await harness.core(hello_params={"heartbeatMs": 100}, auto_answer=False)
    transport = await open_transport(core, harness)
    peer = await core.connection(0)
    # Any answer to the probe keeps the connection, even "method not found".
    probe = await peer.next()
    assert probe["method"] == "ping"
    peer.send({"jsonrpc": "2.0", "id": probe["id"], "error": {"code": -32601, "message": "Method not found"}})
    assert (await peer.next())["method"] == "ping"
    # The second probe goes unanswered.
    closed = await asyncio.wait_for(transport.closed, 5)
    assert closed.code == 4000
    assert closed.retryable is True
    assert (await peer.closed)[0] == 4000
