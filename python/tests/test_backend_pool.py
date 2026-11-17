"""Port of ``typescript/test/backend-pool.test.mjs``."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from backend_support import API_KEY, FakeCore, Harness, Peer, harness, methods, until  # noqa: F401
from tidblink_server._backend_pool import BackendConnectionPool, SessionConnectionEnd
from tidblink_server.errors import BackendConnectionClosedError, BackendRequestError, TiDBLinkError

def attach_answer(message: dict[str, Any]) -> dict[str, Any]:
    return {"generation": 1, "cursor": message["params"].get("cursor", "0")}


async def start(harness: Harness, **options: Any) -> FakeCore:
    return await harness.core(attach_result=attach_answer, **options)


def pool_for(harness: Harness, core: FakeCore, **options: Any) -> BackendConnectionPool:
    pool = BackendConnectionPool(base_url=core.base_url, api_key=API_KEY, idle_close=0.05, **options)
    harness.closers.append(pool.close)
    return pool


class Attachment:
    def __init__(self, session_id: str, **extra: Any) -> None:
        self.session_id = session_id
        self.extra = extra
        self.cursor: str | None = None
        self.attached_to: list[tuple[dict[str, Any], Any]] = []
        self.notes: list[tuple[str, dict[str, Any]]] = []
        self.ends: list[SessionConnectionEnd] = []

    def attach_params(self) -> dict[str, Any]:
        params: dict[str, Any] = {"userId": "user_1", "sessionId": self.session_id}
        if self.cursor is not None:
            params["cursor"] = self.cursor
        return {**params, **self.extra}

    def attached(self, result: dict[str, Any], link: Any) -> None:
        self.attached_to.append((result, link))

    def notify(self, method: str, params: dict[str, Any], link: Any, size: int) -> None:
        self.notes.append((method, params))

    def ended(self, end: SessionConnectionEnd) -> None:
        self.ends.append(end)


def refused(reason: str) -> dict[str, Any]:
    return {"error": {"code": -32000, "message": "Refused", "data": {"reason": reason}}}


async def test_attachments_share_a_connection_up_to_max_sessions_then_open_another(harness: Harness) -> None:
    core = await start(harness, hello_params={"maxSessions": 2})
    pool = pool_for(harness, core)
    sessions = [Attachment(session_id, tools=[]) for session_id in ("s1", "s2", "s3")]
    results = await asyncio.gather(*(pool.attach(a) for a in sessions))
    assert results[0] == {"generation": 1, "cursor": "0"}
    assert pool.connections == 2
    first, second = core.peers
    await until(lambda: len(first.log) + len(second.log) == 3)
    assert sorted((m["params"] for m in first.log + second.log), key=lambda p: p["sessionId"]) == [
        {"userId": "user_1", "sessionId": "s1", "tools": []},
        {"userId": "user_1", "sessionId": "s2", "tools": []},
        {"userId": "user_1", "sessionId": "s3", "tools": []},
    ]
    assert len(first.log) == 2
    for a in sessions:
        assert len(a.attached_to) == 1


async def test_a_connection_full_refusal_moves_the_attach_to_another_connection_and_the_empty_one_closes(
    harness: Harness,
) -> None:
    core = await start(
        harness,
        respond=lambda message, peer, index: (
            refused("connection_full") if message["method"] == "session.attach" and index == 0 else None
        ),
    )
    pool = pool_for(harness, core)
    a = Attachment("s1")
    await pool.attach(a)
    assert len(core.peers) == 2
    assert len(a.attached_to) == 1
    assert (await core.peers[0].closed)[0] == 1000
    assert pool.connections == 1


async def test_notifications_reach_the_attachment_holding_their_session_session_detached_ends_it_without_retry(
    harness: Harness,
) -> None:
    core = await start(harness)
    pool = pool_for(harness, core)
    a = Attachment("s1")
    b = Attachment("s2")
    await pool.attach(a)
    await pool.attach(b)
    peer = core.peers[0]
    peer.send({"jsonrpc": "2.0", "method": "session.event", "params": {"sessionId": "s1", "cursor": "3", "event": {}}})
    peer.send({"jsonrpc": "2.0", "method": "tool.call", "params": {"sessionId": "s9", "callId": "c"}})
    peer.send({"jsonrpc": "2.0", "method": "session.detached", "params": {"sessionId": "s1", "reason": "superseded"}})
    peer.send(
        {"jsonrpc": "2.0", "method": "session.detached", "params": {"sessionId": "s2", "reason": "session_deleted"}}
    )
    await until(lambda: a.ends and b.ends)
    assert a.notes == [("session.event", {"sessionId": "s1", "cursor": "3", "event": {}})]
    assert a.ends == [SessionConnectionEnd("superseded")]
    assert b.ends == [SessionConnectionEnd("session_deleted")]
    await asyncio.sleep(0.1)
    assert methods(peer).count("session.attach") == 2
    # The Session can be connected again.
    await pool.attach(Attachment("s1"))


async def test_a_lost_connection_reattaches_each_session_with_fresh_parameters_on_a_new_connection(
    harness: Harness,
) -> None:
    core = await start(harness)
    pool = pool_for(harness, core)
    a = Attachment("s1")
    await pool.attach(a)
    a.cursor = "7"
    core.peers[0].destroy()
    replacement = await core.connection(1)
    await until(lambda: len(a.attached_to) == 2)
    assert replacement.log[0]["params"] == {"userId": "user_1", "sessionId": "s1", "cursor": "7"}
    assert a.attached_to[1][1].open is True
    assert a.ends == []
    replacement.send({"jsonrpc": "2.0", "method": "session.event", "params": {"sessionId": "s1", "cursor": "8"}})
    await until(lambda: len(a.notes) == 1)


async def test_connection_drain_moves_attachments_to_a_new_connection_ignores_the_old_one_and_closes_it(
    harness: Harness,
) -> None:
    core = await start(harness)
    pool = pool_for(harness, core)
    a = Attachment("s1")
    await pool.attach(a)
    old = core.peers[0]
    old.send({"jsonrpc": "2.0", "method": "connection.drain", "params": {"closeAfterMs": 5000}})
    following = await core.connection(1)
    await until(lambda: len(a.attached_to) == 2)
    assert following.log[0]["method"] == "session.attach"
    old.send({"jsonrpc": "2.0", "method": "session.event", "params": {"sessionId": "s1", "cursor": "1"}})
    following.send({"jsonrpc": "2.0", "method": "session.event", "params": {"sessionId": "s1", "cursor": "2"}})
    await until(lambda: len(a.notes) == 1)
    assert a.notes[0][1]["cursor"] == "2"
    assert (await old.closed)[0] == 1000
    # New attachments avoid the draining connection.
    await pool.attach(Attachment("s2"))
    assert methods(old).count("session.attach") == 1


async def test_a_revoked_key_ends_attachments_for_good_and_refuses_new_ones(harness: Harness) -> None:
    revoked = False

    def on_connection(peer: Peer) -> None:
        if revoked:
            peer.close(4001, "authentication failed")

    core = await start(harness, on_connection=on_connection)
    pool = pool_for(harness, core)
    a = Attachment("s1")
    await pool.attach(a)
    revoked = True
    core.peers[0].close(4001, "revoked")
    await until(lambda: len(a.ends) == 1)
    assert a.ends[0].reason == "error"
    assert isinstance(a.ends[0].error, BackendConnectionClosedError)
    assert a.ends[0].error.code == 4001
    with pytest.raises(BackendConnectionClosedError) as error:
        await pool.attach(Attachment("s2"))
    assert error.value.code == 4001


async def test_a_refused_first_attach_raises_and_one_session_connects_once_per_client(harness: Harness) -> None:
    core = await start(
        harness,
        respond=lambda message, peer, index: (
            refused("session_not_found") if message.get("params", {}).get("sessionId") == "missing" else None
        ),
    )
    pool = pool_for(harness, core)
    with pytest.raises(BackendRequestError) as error:
        await pool.attach(Attachment("missing"))
    assert error.value.reason == "session_not_found"
    first = asyncio.ensure_future(pool.attach(Attachment("s1")))
    await asyncio.sleep(0)
    with pytest.raises(TiDBLinkError, match="already connected"):
        await pool.attach(Attachment("s1"))
    await first
    # A refused Session can be tried again.
    with pytest.raises(BackendRequestError):
        await pool.attach(Attachment("missing"))


async def test_detach_releases_the_session_and_a_connection_that_holds_nothing_closes(harness: Harness) -> None:
    core = await start(harness)
    pool = pool_for(harness, core)
    a = Attachment("s1")
    await pool.attach(a)
    await pool.detach(a)
    peer = core.peers[0]
    assert peer.log[-1] == {
        "jsonrpc": "2.0",
        "id": peer.log[-1]["id"],
        "method": "session.detach",
        "params": {"sessionId": "s1"},
    }
    assert (await peer.closed)[0] == 1000
    assert pool.connections == 0
    assert a.ends == []


async def test_request_retries_an_idempotent_request_on_a_new_connection_after_a_lost_one(harness: Harness) -> None:
    def respond(message: dict[str, Any], peer: Peer, index: int) -> Any:
        if message["method"] != "tool.result":
            return None
        if index == 0:
            peer.destroy()
            return "silent"
        return {"result": {"accepted": True}}

    core = await start(harness, respond=respond)
    pool = pool_for(harness, core)
    params = {"callId": "call_1", "output": "done"}
    assert await pool.request("tool.result", params, retry_until=time.time() + 5) == {"accepted": True}
    assert core.peers[1].log[-1]["params"] == params

    # Without retry_until a lost connection is the caller's to handle.
    def lose(message: dict[str, Any], peer: Peer, index: int) -> Any:
        if message["method"] != "tool.result":
            return None
        peer.destroy()
        return "silent"

    lone = await start(harness, respond=lose)
    with pytest.raises(BackendConnectionClosedError):
        await pool_for(harness, lone).request("tool.result", params)


async def test_close_detaches_every_session_ends_them_as_closed_and_refuses_later_attaches(harness: Harness) -> None:
    core = await start(harness)
    pool = pool_for(harness, core)
    a = Attachment("s1")
    await pool.attach(a)
    await pool.close()
    assert a.ends == [SessionConnectionEnd("closed")]
    assert "session.detach" in methods(core.peers[0])
    assert (await core.peers[0].closed)[0] == 1000
    with pytest.raises(TiDBLinkError, match="closed"):
        await pool.attach(Attachment("s2"))
