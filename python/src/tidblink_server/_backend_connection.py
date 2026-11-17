"""One Backend Connection (``backend-connection.ts`` in the TypeScript SDK).

A WebSocket from this Application Server process to the core, carrying one
JSON-RPC 2.0 message per text frame. The published definition is the core's
``docs/backend-connection-v1.schema.json``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

from .errors import BackendConnectionClosedError, BackendRequestError, TiDBLinkError

BACKEND_CONNECTION_PATH = "/api/v1/org/backend/ws"
BACKEND_CONNECTION_SUBPROTOCOL = "tidb-link.backend.v1"
#: Close code the SDK uses when the core stopped answering liveness probes.
CLIENT_HEARTBEAT_CLOSE = 4000
DEFAULT_OPEN_TIMEOUT = 15.0
#: Bounds the liveness interval, in seconds, whatever ``hello.heartbeatMs`` says.
MIN_PROBE_INTERVAL = 1.0
MAX_PROBE_INTERVAL = 60.0
#: How long the default WebSocket waits for the core's half of a closing handshake.
_CLOSE_TIMEOUT = 2.0


class BackendWebSocket(Protocol):
    """The part of a client WebSocket the SDK uses (a ``websockets`` client connection fits).

    ``recv`` returns the next message and raises once the connection is
    closed; ``close_code`` and ``close_reason`` then tell why (``None`` for
    a connection that vanished).
    """

    @property
    def close_code(self) -> int | None: ...

    @property
    def close_reason(self) -> str | None: ...

    async def send(self, message: str) -> Any: ...

    async def recv(self) -> str | bytes: ...

    async def close(self, code: int = 1000, reason: str = "") -> Any: ...


class WebSocketConnect(Protocol):
    """Opens a client WebSocket with these subprotocols and upgrade headers.

    The SDK sends the Organization API Key in the ``authorization`` header,
    never in the URL. Replace it to use a proxy, another WebSocket library or
    a test double; the default uses ``websockets``.
    """

    def __call__(
        self, url: str, *, subprotocols: Sequence[str], headers: Mapping[str, str]
    ) -> Awaitable[BackendWebSocket]: ...


async def default_websocket_connect(
    url: str, *, subprotocols: Sequence[str], headers: Mapping[str, str]
) -> BackendWebSocket:
    """Open a WebSocket with the ``websockets`` library."""
    from websockets.asyncio.client import connect
    from websockets.typing import Subprotocol

    return await connect(  # type: ignore[return-value]
        url,
        subprotocols=[Subprotocol(value) for value in subprotocols],
        additional_headers=dict(headers),
        # The core may send frames larger than the library's default limit.
        max_size=None,
        # Liveness is the transport's own: see BackendTransport.
        ping_interval=None,
        # The transport bounds the whole open, hello included.
        open_timeout=None,
        close_timeout=_CLOSE_TIMEOUT,
    )


@dataclass(frozen=True)
class BackendConnectionHello:
    """Connection parameters the core announces in ``hello``."""

    connection_id: str
    #: Most Session Attachments this connection may hold.
    max_sessions: int
    #: How long a published Client Tool call waits for its claim.
    claim_window_ms: int
    #: Interval of the core's WebSocket pings.
    heartbeat_ms: int
    #: Largest frame the core accepts.
    max_frame_bytes: int


#: Receives a core notification: method, params, and the UTF-8 size of its frame.
BackendNotificationHandler = Callable[[str, dict[str, Any], int], None]


def _is_positive_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 < value <= 2**53 - 1


def _parse_hello(params: object) -> BackendConnectionHello | None:
    if not isinstance(params, dict):
        return None
    connection_id = params.get("connectionId")
    fields = ("maxSessions", "claimWindowMs", "heartbeatMs", "maxFrameBytes")
    if not isinstance(connection_id, str) or not connection_id:
        return None
    if not all(_is_positive_integer(params.get(name)) for name in fields):
        return None
    return BackendConnectionHello(
        connection_id=connection_id,
        max_sessions=params["maxSessions"],
        claim_window_ms=params["claimWindowMs"],
        heartbeat_ms=params["heartbeatMs"],
        max_frame_bytes=params["maxFrameBytes"],
    )


def backend_connection_url(base_url: str) -> str:
    """Derive the Backend Connection URL from the SDK's HTTP(S) base URL."""
    parts = urlsplit(base_url)
    scheme = "wss" if parts.scheme == "https" else "ws"
    path = (parts.path or "/").rstrip("/")
    return urlunsplit((scheme, parts.netloc, f"{path}{BACKEND_CONNECTION_PATH}", "", ""))


def _safe_parse(data: object) -> dict[str, Any] | None:
    if not isinstance(data, str):
        return None
    try:
        value = json.loads(data)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _dump(message: object) -> str:
    return json.dumps(message, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


async def _close_quietly(socket: BackendWebSocket, code: int, reason: str = "") -> None:
    # A client may only send 1000 or an application code in 3000-4999.
    try:
        await socket.close(code if code == 1000 or 3000 <= code <= 4999 else 1000, reason)
    except Exception:
        pass  # Already closing.


def report_unhandled(error: BaseException, message: str = "Unhandled error in a TiDB Link Backend Connection") -> None:
    """Hand an error nobody can receive to the event loop's exception handler.

    The asyncio counterpart of rethrowing it asynchronously.
    """
    asyncio.get_running_loop().call_exception_handler({"message": message, "exception": error})


def silence(future: asyncio.Future[Any]) -> asyncio.Future[Any]:
    """Mark the outcome of a request nobody awaits as handled."""
    future.add_done_callback(lambda done: done.cancelled() or done.exception())
    return future


class _Pending:
    __slots__ = ("future", "on_answer")

    def __init__(self, future: asyncio.Future[Any], on_answer: Callable[[Any], None] | None) -> None:
        self.future = future
        self.on_answer = on_answer


_STOP = object()


class BackendTransport:
    """One open Backend Connection: request and answer pairing, notifications, liveness and close.

    It never reconnects; the connection pool replaces a closed transport.

    Liveness: the core pings at the WebSocket layer, which the WebSocket
    answers without telling the SDK. So when nothing has arrived for one
    heartbeat interval, the transport sends a ``ping`` request; any answer,
    even "method not found", proves the connection alive. With no frame for a
    further interval it closes the connection.
    """

    def __init__(
        self,
        socket: BackendWebSocket,
        hello: BackendConnectionHello,
        on_notification: BackendNotificationHandler,
    ) -> None:
        self._loop = asyncio.get_running_loop()
        self._socket = socket
        self.hello = hello
        self._on_notification: BackendNotificationHandler = on_notification
        self._pending: dict[int, _Pending] = {}
        self._next_id = 1
        self._last_frame_at = self._loop.time()
        self._probing = False
        self._error: BackendConnectionClosedError | None = None
        #: Resolves once with why the connection ended, local closes included.
        self.closed: asyncio.Future[BackendConnectionClosedError] = self._loop.create_future()
        self._outgoing: asyncio.Queue[Any] = asyncio.Queue()
        self._interval = min(MAX_PROBE_INTERVAL, max(MIN_PROBE_INTERVAL, hello.heartbeat_ms / 1000))
        self._timer: asyncio.TimerHandle | None = self._loop.call_later(
            self._interval, self._check_liveness
        )
        self._reader = asyncio.ensure_future(self._read())
        self._writer = asyncio.ensure_future(self._write())

    @classmethod
    async def connect(
        cls,
        *,
        url: str,
        api_key: str,
        on_notification: BackendNotificationHandler,
        websocket_connect: WebSocketConnect | None = None,
        open_timeout: float | None = None,
    ) -> BackendTransport:
        """Open a connection and wait for the core's ``hello``.

        ``open_timeout`` (default 15 seconds) bounds the whole open.
        """
        connect: WebSocketConnect = websocket_connect or default_websocket_connect
        timeout = DEFAULT_OPEN_TIMEOUT if open_timeout is None else open_timeout
        socket: BackendWebSocket | None = None
        try:
            async with asyncio.timeout(timeout):
                try:
                    socket = await connect(
                        url,
                        subprotocols=[BACKEND_CONNECTION_SUBPROTOCOL],
                        headers={"authorization": f"Bearer {api_key}"},
                    )
                except Exception as cause:
                    raise BackendConnectionClosedError(
                        code=1006, message="Could not open a Backend Connection", cause=cause
                    ) from cause
                try:
                    data = await socket.recv()
                except Exception as cause:
                    raise BackendConnectionClosedError(
                        code=socket.close_code or 1006, reason=socket.close_reason or "", cause=cause
                    ) from cause
        except TimeoutError as cause:
            if socket is not None:
                await _close_quietly(socket, 1000)
            raise BackendConnectionClosedError(
                code=1006,
                message=f"Backend Connection sent no hello within {round(timeout * 1000)} ms",
                cause=cause,
            ) from cause
        except BaseException:
            if socket is not None and not isinstance(socket.close_code, int):
                await _close_quietly(socket, 1000)
            raise
        message = _safe_parse(data)
        hello = (
            _parse_hello(message.get("params"))
            if message is not None and message.get("method") == "hello" and "id" not in message
            else None
        )
        if hello is None:
            await _close_quietly(socket, 1002)
            raise BackendConnectionClosedError(
                code=1002, message="Backend Connection did not start with a valid hello"
            )
        return cls(socket, hello, on_notification)

    @property
    def open(self) -> bool:
        """Whether requests can still be sent."""
        return self._error is None

    @property
    def pending_requests(self) -> int:
        """Requests sent and not yet answered."""
        return len(self._pending)

    def request(
        self,
        method: str,
        params: Mapping[str, Any],
        on_answer: Callable[[Any], None] | None = None,
    ) -> asyncio.Future[Any]:
        """Send a request; the future resolves with its answer.

        A JSON-RPC error fails it with :class:`BackendRequestError`; a
        connection that ends first fails it with its
        :class:`BackendConnectionClosedError`, and the request's outcome is
        then unknown. ``on_answer`` sees a successful answer before any frame
        that followed it is handled.
        """
        future: asyncio.Future[Any] = self._loop.create_future()
        if self._error is not None:
            future.set_exception(self._error)
            return future
        request_id = self._next_id
        self._next_id += 1
        text = _dump({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        if len(text.encode()) > self.hello.max_frame_bytes:
            future.set_exception(
                TiDBLinkError(
                    f"{method} exceeds the Backend Connection frame limit of {self.hello.max_frame_bytes} bytes"
                )
            )
            return future
        self._pending[request_id] = _Pending(future, on_answer)
        self._outgoing.put_nowait((text, request_id))
        return future

    def close(self, code: int = 1000, reason: str = "") -> None:
        """Close the connection. Pending requests fail at once."""
        if self._error is not None:
            return
        self._end(
            BackendConnectionClosedError(
                code=code, reason=reason, message=reason or "Backend Connection closed by this process"
            )
        )
        self._outgoing.put_nowait(("close", code, reason))

    async def wait_closed(self) -> None:
        """Wait until the socket closed and the transport's tasks finished."""
        await asyncio.gather(self._reader, self._writer, return_exceptions=True)

    def abort(self) -> None:
        """Stop the transport's tasks without waiting for the socket."""
        self._reader.cancel()
        self._writer.cancel()

    async def _read(self) -> None:
        socket = self._socket
        try:
            while True:
                try:
                    data = await socket.recv()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    break
                self._receive(data)
        finally:
            self._end(
                BackendConnectionClosedError(
                    code=socket.close_code or 1006, reason=socket.close_reason or ""
                )
            )
            self._outgoing.put_nowait(_STOP)

    async def _write(self) -> None:
        while True:
            item = await self._outgoing.get()
            if item is _STOP:
                return
            if item[0] == "close":
                await _close_quietly(self._socket, item[1], item[2])
                return
            text, request_id = item
            try:
                await self._socket.send(text)
            except Exception as cause:
                pending = self._pending.pop(request_id, None) if request_id is not None else None
                if pending is not None and not pending.future.done():
                    pending.future.set_exception(
                        BackendConnectionClosedError(
                            code=1006, message="Could not send on the Backend Connection", cause=cause
                        )
                    )

    def _send(self, message: object) -> None:
        self._outgoing.put_nowait((_dump(message), None))

    def _receive(self, data: object) -> None:
        if self._error is not None:
            return
        self._last_frame_at = self._loop.time()
        self._probing = False
        message = _safe_parse(data)
        if message is None or message.get("jsonrpc") != "2.0":
            self.close(1002, "invalid frame")
            return
        method = message.get("method")
        if isinstance(method, str):
            if "id" in message:
                # The core sends no requests in this protocol revision.
                self._send(
                    {
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "error": {"code": -32601, "message": "Method not found"},
                    }
                )
                return
            params = message.get("params")
            try:
                self._on_notification(
                    method, params if isinstance(params, dict) else {}, len(data.encode())  # type: ignore[union-attr]
                )
            except Exception as error:
                report_unhandled(error)
            return
        request_id = message.get("id")
        if not isinstance(request_id, int) or isinstance(request_id, bool):
            return
        pending = self._pending.pop(request_id, None)
        if pending is None:
            return
        error = message.get("error")
        if isinstance(error, dict):
            code = error.get("code")
            text = error.get("message")
            data_ = error.get("data")
            reason = data_.get("reason") if isinstance(data_, dict) else None
            if not pending.future.done():
                pending.future.set_exception(
                    BackendRequestError(
                        code=code if isinstance(code, int) and not isinstance(code, bool) else -32603,
                        message=text if isinstance(text, str) and text else "Backend Connection request failed",
                        reason=reason if isinstance(reason, str) else None,
                        details=data_.get("details") if isinstance(data_, dict) else None,
                    )
                )
            return
        result = message.get("result")
        # Frames that arrived with the answer are handled after `on_answer`.
        try:
            if pending.on_answer is not None:
                pending.on_answer(result)
        except Exception as error:
            report_unhandled(error)
        finally:
            if not pending.future.done():
                pending.future.set_result(result)

    def _check_liveness(self) -> None:
        self._timer = None
        if self._error is not None:
            return
        self._timer = self._loop.call_later(self._interval, self._check_liveness)
        if self._loop.time() - self._last_frame_at < self._interval:
            return
        if self._probing:
            self._end(
                BackendConnectionClosedError(
                    code=CLIENT_HEARTBEAT_CLOSE,
                    message=f"Backend Connection was silent for {round(2 * self._interval * 1000)} ms",
                )
            )
            self._outgoing.put_nowait(("close", CLIENT_HEARTBEAT_CLOSE, "heartbeat timeout"))
            return
        self._probing = True
        # Any answer, including "method not found" from a core without `ping`, counts.
        silence(self.request("ping", {}))

    def _end(self, error: BackendConnectionClosedError) -> None:
        if self._error is not None:
            return
        self._error = error
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self._on_notification = lambda *_: None
        pending = list(self._pending.values())
        self._pending.clear()
        for entry in pending:
            if not entry.future.done():
                entry.future.set_exception(error)
        if not self.closed.done():
            self.closed.set_result(error)

