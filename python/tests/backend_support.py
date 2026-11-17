"""A stand-in core for Backend Connection tests (``test/support/backend-server.mjs`` in the TypeScript SDK).

A real local ``websockets`` server: each upgraded connection is a :class:`Peer`
that sends ``hello`` first, records every client message in ``peer.log`` and
answers through ``respond(message, peer, index)``; by default it answers
``session.attach``, ``session.detach`` and ``ping``. REST calls of the same
core go through ``httpx.MockTransport`` (see :meth:`Harness.client`).
"""

from __future__ import annotations

import asyncio
import gc
import inspect
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request

from tidblink_server import OrganizationClient

API_KEY = "ti_org_backend_" + "x" * 43


def hello(**overrides: Any) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "method": "hello",
        "params": {
            "connectionId": f"bconn_{'0' * 32}",
            "maxSessions": 1024,
            "claimWindowMs": 5000,
            "heartbeatMs": 20000,
            "maxFrameBytes": 4 * 1024 * 1024,
            **overrides,
        },
    }


class Peer:
    """One client connection, seen from the core."""

    def __init__(self, socket: ServerConnection, index: int) -> None:
        self.socket = socket
        self.index = index
        self.request: Request = socket.request  # type: ignore[assignment]
        #: Every message the client sent, in order.
        self.log: list[dict[str, Any]] = []
        self._messages: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._outgoing: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        #: Resolves with ``(code, reason)`` of the close frame the client sent, or 1006 when it vanished.
        self.closed: asyncio.Future[tuple[int, str]] = asyncio.get_running_loop().create_future()

    @property
    def path(self) -> str:
        return self.request.path

    def send(self, value: Any) -> None:
        """Send a message after those sent before it."""
        self._outgoing.put_nowait(("send", value if isinstance(value, str) else json.dumps(value, separators=(",", ":"))))

    def close(self, code: int = 1000, reason: str = "") -> None:
        self._outgoing.put_nowait(("close", (code, reason)))

    def destroy(self) -> None:
        """Drop the TCP connection without a close frame."""
        self._outgoing.put_nowait(("destroy", None))

    async def next(self) -> dict[str, Any]:
        """The next JSON message from the client."""
        return await self._messages.get()

    async def _write(self) -> None:
        while True:
            kind, value = await self._outgoing.get()
            try:
                if kind == "send":
                    await self.socket.send(value)
                elif kind == "close":
                    await self.socket.close(*value)
                else:
                    self.socket.transport.abort()
            except ConnectionClosed:
                pass

    async def _read(self) -> None:
        try:
            async for data in self.socket:
                message = json.loads(data)
                self.log.append(message)
                self._messages.put_nowait(message)
        except ConnectionClosed:
            pass
        rcvd = self.socket.protocol.close_rcvd
        if not self.closed.done():
            self.closed.set_result((rcvd.code, rcvd.reason) if rcvd is not None else (1006, ""))


Respond = Callable[[dict[str, Any], Peer, int], Any]


def default_answer(message: dict[str, Any], attach_result: dict[str, Any] | None = None) -> dict[str, Any] | None:
    method = message.get("method")
    if method == "session.attach":
        return {"result": attach_result or {"generation": 4, "cursor": "10"}}
    if method == "session.detach":
        return {"result": {}}
    if method == "ping":
        return {"error": {"code": -32601, "message": "Method not found"}}
    return None


class FakeCore:
    def __init__(
        self,
        *,
        respond: Respond | None,
        on_connection: Callable[[Peer], None] | None,
        send_hello: bool,
        hello_params: dict[str, Any] | None,
        reject: Callable[[Request], int | None] | None,
        auto_answer: bool,
        attach_result: Callable[[dict[str, Any]], dict[str, Any]] | None,
    ) -> None:
        self._respond = respond
        self._on_connection = on_connection
        self._send_hello = send_hello
        self._hello_params = hello_params or {}
        self._reject = reject
        self._auto_answer = auto_answer
        self._attach_result = attach_result
        self.peers: list[Peer] = []
        self._waiting: list[asyncio.Future[Peer]] = []
        self._tasks: set[asyncio.Task[Any]] = set()
        self._server: Any = None
        self.port = 0

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/api/v1/org/backend/ws"

    async def start(self) -> FakeCore:
        def process_request(connection: ServerConnection, request: Request) -> Any:
            status = self._reject(request) if self._reject else None
            if status:
                return connection.respond(status, "")
            return None

        self._server = await serve(
            self._handle,
            "127.0.0.1",
            0,
            subprotocols=["tidb-link.backend.v1"],  # type: ignore[list-item]
            process_request=process_request,
            ping_interval=None,
            max_size=None,
            compression=None,
        )
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def connection(self, index: int | None = None) -> Peer:
        """The connection at ``index`` once made, or the next one."""
        if index is not None:
            while len(self.peers) <= index:
                await asyncio.sleep(0.005)
            return self.peers[index]
        waiter: asyncio.Future[Peer] = asyncio.get_running_loop().create_future()
        self._waiting.append(waiter)
        return await waiter

    async def _handle(self, socket: ServerConnection) -> None:
        peer = Peer(socket, len(self.peers))
        self.peers.append(peer)
        writer = asyncio.ensure_future(peer._write())
        reader = asyncio.ensure_future(peer._read())
        answering = asyncio.ensure_future(self._answer(peer)) if self._auto_answer else None
        if self._send_hello:
            peer.send(hello(**self._hello_params))
        if self._on_connection:
            self._on_connection(peer)
        if self._waiting:
            self._waiting.pop(0).set_result(peer)
        try:
            await reader
            # Let queued frames go before the handler returns and the server closes the socket.
            await asyncio.sleep(0.01)
        finally:
            for task in (writer, answering):
                if task is not None:
                    task.cancel()
            await asyncio.gather(writer, *(t for t in (answering,) if t), return_exceptions=True)

    async def _answer(self, peer: Peer) -> None:
        while True:
            message = await peer.next()
            answer = self._respond(message, peer, peer.index) if self._respond else None
            if inspect.isawaitable(answer):
                answer = await answer
            if answer is None:
                attach = self._attach_result(message) if self._attach_result and message.get("method") == "session.attach" else None
                answer = default_answer(message, attach)
            if answer is not None and answer != "silent":
                peer.send({"jsonrpc": "2.0", "id": message.get("id"), **answer})

    async def close(self) -> None:
        for peer in self.peers:
            peer.destroy()
        await asyncio.sleep(0)
        if self._server is not None:
            self._server.close(close_connections=False)
            for peer in self.peers:
                if peer.socket.transport is not None:
                    peer.socket.transport.abort()
            await self._server.wait_closed()


@dataclass
class HttpCall:
    method: str
    path: str
    body: bytes

    def __str__(self) -> str:
        return f"{self.method} {self.path}"


@dataclass
class Harness:
    """Starts stand-in cores and clients and closes them after the test."""

    cores: list[FakeCore] = field(default_factory=list)
    #: What reached the event loop's exception handler; tests that expect some clear it.
    unhandled: list[dict[str, Any]] = field(default_factory=list)
    clients: list[OrganizationClient] = field(default_factory=list)
    closers: list[Callable[[], Awaitable[Any]]] = field(default_factory=list)

    async def core(
        self,
        *,
        respond: Respond | None = None,
        on_connection: Callable[[Peer], None] | None = None,
        send_hello: bool = True,
        hello_params: dict[str, Any] | None = None,
        reject: Callable[[Request], int | None] | None = None,
        auto_answer: bool = True,
        attach_result: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> FakeCore:
        core = await FakeCore(
            respond=respond,
            on_connection=on_connection,
            send_hello=send_hello,
            hello_params=hello_params,
            reject=reject,
            auto_answer=auto_answer,
            attach_result=attach_result,
        ).start()
        self.cores.append(core)
        return core

    def http_client(
        self,
        http: Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]] | None = None,
        calls: list[HttpCall] | None = None,
    ) -> httpx.AsyncClient:
        """An HTTP client whose requests go to ``http`` and are recorded in ``calls``."""

        async def transport(request: httpx.Request) -> httpx.Response:
            body = await request.aread()
            if calls is not None:
                calls.append(HttpCall(request.method, request.url.raw_path.decode(), body))
            if http is None:
                return httpx.Response(404)
            response = http(request)
            if inspect.isawaitable(response):
                response = await response
            return response

        client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
        self.closers.append(client.aclose)
        return client

    def client(
        self,
        core: FakeCore,
        http: Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]] | None = None,
        calls: list[HttpCall] | None = None,
        api_key: str = API_KEY,
    ) -> OrganizationClient:
        """An OrganizationClient whose Backend Connections go to ``core`` and REST calls to ``http``."""
        client = OrganizationClient(base_url=core.base_url, api_key=api_key, http_client=self.http_client(http, calls))
        self.clients.append(client)
        return client

    async def close(self) -> None:
        await asyncio.gather(*(client.close() for client in self.clients))
        for closer in self.closers:
            await closer()
        await asyncio.gather(*(core.close() for core in self.cores))


def _sdk_task(task: asyncio.Task[Any]) -> bool:
    code = getattr(task.get_coro(), "cr_code", None)
    return code is not None and "tidblink_server" in code.co_filename


@pytest.fixture
async def harness() -> Any:
    instance = Harness()
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(lambda _, context: instance.unhandled.append(context))
    yield instance
    await instance.close()
    gc.collect()
    await asyncio.sleep(0)
    loop.set_exception_handler(None)
    assert instance.unhandled == [], instance.unhandled
    # Nothing the SDK started may outlive OrganizationClient.close().
    current = asyncio.current_task()
    leftover = [task for task in asyncio.all_tasks() if task is not current and not task.done() and _sdk_task(task)]
    assert leftover == [], leftover


async def until(predicate: Callable[[], Any], timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


def methods(peer: Peer) -> list[str]:
    return [message.get("method") for message in peer.log]


def by_method(peer: Peer, method: str) -> list[dict[str, Any]]:
    return [message for message in peer.log if message.get("method") == method]


def deadline_in(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def json_response(value: Any, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=value)


# A Turn's events and records, as the session-send and lamp tests use them -------------

AT = "2026-11-13T00:00:00.000Z"
DONE = "2026-11-13T00:00:02.000Z"


def receipt(**overrides: Any) -> dict[str, Any]:
    return {
        "inputId": "msg_1",
        "sequence": 1,
        "kind": "message",
        "mode": "started",
        "turnId": "turn_1",
        "message": {"id": "um_1", "role": "user", "content": "Too bright", "createdAt": AT},
        "createdAt": AT,
        "appliedAt": None,
        "rejection": None,
        "result": {"status": "running", "completedAt": None},
        **overrides,
    }


def settled(**overrides: Any) -> dict[str, Any]:
    return receipt(**{"appliedAt": AT, "result": {"status": "succeeded", "completedAt": DONE}, **overrides})


SAVED_TURN = {
    "id": "turn_1",
    "sessionId": "sess_1",
    "status": "succeeded",
    "billingTag": None,
    "userMessage": {"id": "um_1", "role": "user", "content": "Too bright", "sessionId": "sess_1", "turnId": "turn_1", "createdAt": AT},
    "operations": [],
    "assistantMessage": {
        "id": "am_1",
        "role": "assistant",
        "content": "Dimmed to 40%.",
        "sessionId": "sess_1",
        "turnId": "turn_1",
        "createdAt": DONE,
    },
    "createdAt": AT,
    "completedAt": DONE,
}


def rest(interaction: dict[str, Any] | None = None) -> Callable[[httpx.Request], httpx.Response]:
    """REST of the stand-in core: the settled interaction and its saved Turn."""

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/turns/turn_1"):
            return json_response({"turn": SAVED_TURN})
        if "/interactions/" in path:
            return json_response({"interaction": interaction or settled()})
        return httpx.Response(404)

    return handle


def turn_event(cursor: str, type_: str, event: str, payload: dict[str, Any], turn_id: str = "turn_1") -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "method": "session.event",
        "params": {
            "sessionId": "sess_1",
            "cursor": cursor,
            "event": {
                "type": type_,
                "sessionId": "sess_1",
                "turnId": turn_id,
                "createdAt": AT,
                "data": {
                    "event": event,
                    "turnId": turn_id,
                    "sessionId": "sess_1",
                    "seq": int(cursor),
                    "createdAt": AT,
                    "payload": payload,
                },
            },
        },
    }


def draft(cursor: str, text: str, turn_id: str = "turn_1") -> dict[str, Any]:
    return turn_event(cursor, "agent.session.turn.output_text.snapshot", "assistant_draft", {"text": text}, turn_id)


def finished(cursor: str, turn_id: str = "turn_1") -> dict[str, Any]:
    return turn_event(cursor, "agent.session.turn.completed", "turn_finished", {"status": "succeeded"}, turn_id)


def idle(cursor: str, turn_id: str = "turn_1") -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "method": "session.event",
        "params": {
            "sessionId": "sess_1",
            "cursor": cursor,
            "event": {
                "type": "agent.session.idle",
                "sessionId": "sess_1",
                "turnId": turn_id,
                "createdAt": DONE,
                "data": {"id": f"idle_{cursor}", "turnId": turn_id, "createdAt": DONE},
            },
        },
    }
