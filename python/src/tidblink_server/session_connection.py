"""A Session attached over this process's Backend Connections (``session-connection.ts`` in the TypeScript SDK)."""

from __future__ import annotations

import asyncio
import inspect
import re
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ._backend_connection import report_unhandled
from ._backend_pool import BackendConnectionPool, BackendLink, SessionConnectionEnd
from ._client_tool_executor import ClientToolCallContext, ClientToolExecutor, ClientToolResultRefusedError
from ._http import HttpClient, require_known_keys, segment
from ._types import OMIT, Omittable
from .client_tools import ClientToolDeclaration, validate_client_tools
from .errors import TiDBLinkApiError, TiDBLinkError
from .events import (
    NotifiedFile,
    RealtimeResponse,
    RealtimeTranscript,
    SessionEvent,
    as_known_session_event,
    session_event_from,
)
from .realtime import (
    RealtimeConnectGrant,
    RealtimeOpenAudio,
    require_open_realtime_connection_receipt,
    require_realtime_connect_grant_receipt,
)
from .session_send import send_over_connection

if TYPE_CHECKING:
    from .history import BillingTag
    from .turn import TurnResult

__all__ = [
    "ClientToolCallContext",
    "ClientToolHandler",
    "ClientToolResultRefusedError",
    "SessionConnection",
    "SessionConnectionEnd",
    "connect_session",
]

DEFAULT_MAX_BUFFERED_BYTES = 8 * 1024 * 1024
_DECIMAL = re.compile(r"0|[1-9][0-9]*")
_CURSOR = re.compile(r"0|[1-9][0-9]{0,14}")
_TOOL_NAME = re.compile(r"[a-zA-Z0-9_-]{1,64}")


@dataclass(frozen=True)
class ClientToolHandler:
    """One Client Tool or Connector function: its declaration, which the model sees, and the function that executes it.

    ``run(args, context)`` executes one call, usually by forwarding it to a
    device; it may be sync or async. Return the text the model receives;
    other values are sent as JSON. Raise to report a failure, whose message
    the model sees. At the call's deadline the SDK cancels the run's task.
    """

    description: str
    #: JSON Schema of the arguments.
    parameters: Mapping[str, Any]
    run: Callable[[dict[str, Any], ClientToolCallContext], Any]


#: The Session's Client Tools by name: ``ClientToolHandler``s, or mappings with
#: ``description``, ``parameters`` and ``run``.
ClientToolHandlers = Mapping[str, "ClientToolHandler | Mapping[str, Any]"]

Callback = Callable[[Any], Awaitable[None] | None]


def _not_after(cursor: str, last: str | None) -> bool:
    """Whether ``cursor`` is at or before ``last``; opaque cursors never compare."""
    return (
        last is not None
        and _DECIMAL.fullmatch(cursor) is not None
        and _DECIMAL.fullmatch(last) is not None
        and int(cursor) <= int(last)
    )


class _Attachment:
    """The pool's view of a Session connection."""

    __slots__ = ("_connection", "session_id")

    def __init__(self, connection: SessionConnection) -> None:
        self._connection = connection
        self.session_id = connection.session_id

    def attach_params(self) -> dict[str, Any]:
        return self._connection._attach_params()

    def attached(self, result: dict[str, Any], link: BackendLink) -> None:
        self._connection._attached(result, link)

    def notify(self, method: str, params: dict[str, Any], link: BackendLink, size: int) -> None:
        self._connection._notify(method, params, link, size)

    def ended(self, end: SessionConnectionEnd) -> None:
        self._connection._end(end)


class _Watcher:
    __slots__ = ("events", "waiter")

    def __init__(self) -> None:
        self.events: deque[SessionEvent] = deque()
        self.waiter: asyncio.Future[None] | None = None

    def wake(self) -> None:
        if self.waiter is not None and not self.waiter.done():
            self.waiter.set_result(None)


class _EventWatch:
    def __init__(self, connection: SessionConnection, watcher: _Watcher) -> None:
        self._connection = connection
        self._watcher = watcher

    async def next(self) -> SessionEvent | None:
        watcher = self._watcher
        while True:
            if watcher.events:
                return watcher.events.popleft()
            if self._connection._ended is not None:
                return None
            watcher.waiter = asyncio.get_running_loop().create_future()
            try:
                await watcher.waiter
            finally:
                watcher.waiter = None

    def stop(self) -> None:
        self._connection._watchers.discard(self._watcher)


class _Channel:
    """What ``send()`` needs from its connection."""

    def __init__(self, connection: SessionConnection) -> None:
        self._connection = connection
        self.session_id = connection.session_id
        self.http = connection._http
        self.closed = connection._closed

    async def link(self, after: BackendLink | None = None) -> BackendLink:
        return await self._connection._next_link(after)

    def report(self, error: BaseException) -> None:
        self._connection._report(error)

    def watch(self) -> _EventWatch:
        return self._connection._watch()


_STOP = object()


class SessionConnection:
    """A Session attached over this process's Backend Connections.

    It receives the Session's events, in order and without gaps, across
    reconnects and replica moves, until :meth:`close` or until ``closed``
    reports why it ended. Use ``Session.connect()`` to open one.
    """

    def __init__(
        self,
        pool: BackendConnectionPool,
        http: HttpClient,
        user_id: str,
        session_id: str,
        *,
        handlers: Mapping[str, ClientToolHandler] | None,
        declarations: list[ClientToolDeclaration] | None,
        observe: bool,
        cursor: str | None,
        on_cursor: Callback | None,
        on_event: Callback | None,
        on_transcript: Callback | None,
        on_response: Callback | None,
        on_file: Callback | None,
        max_buffered_bytes: int | None,
        on_error: Callable[[BaseException], None] | None,
    ) -> None:
        loop = asyncio.get_running_loop()
        self._pool = pool
        self._http = http
        self.user_id = user_id
        self.session_id = session_id
        self._observe = observe
        self._declarations = declarations
        self._cursor = cursor
        self._generation: int | None = None
        self._on_cursor = on_cursor
        self._on_event = on_event
        self._on_transcript = on_transcript
        self._on_response = on_response
        self._on_file = on_file
        self._on_error = on_error
        self._max_buffered = (
            (max_buffered_bytes or DEFAULT_MAX_BUFFERED_BYTES) if observe else float("inf")
        )
        self._link: BackendLink | None = None
        self._ended: SessionConnectionEnd | None = None
        self._closed: asyncio.Future[SessionConnectionEnd] = loop.create_future()
        #: Deliveries, run one at a time once ``connect()`` handed the connection to its caller.
        self._jobs: asyncio.Queue[Any] = asyncio.Queue()
        self._worker: asyncio.Task[None] | None = None
        #: Size of the events waiting for the callbacks.
        self._buffered = 0
        #: Dropping events until an attach replays them from ``_cursor``.
        self._behind = False
        self._link_waiters: list[asyncio.Future[None]] = []
        #: Event buffers of ``send()`` calls in flight.
        self._watchers: set[_Watcher] = set()
        # An executing connection runs the Session's calls, declared here or not.
        self._executor = (
            None
            if observe
            else ClientToolExecutor(
                pool=pool, session_id=session_id, handlers=handlers or {}, report=self._report
            )
        )
        self._attachment = _Attachment(self)

    def __repr__(self) -> str:
        return f"SessionConnection(session_id={self.session_id!r})"

    @property
    def closed(self) -> asyncio.Future[SessionConnectionEnd]:
        """Resolves once, with why the connection ended.

        Awaiting it with a timeout, or cancelling a task that awaits it, does
        not affect the connection.
        """
        return asyncio.shield(self._closed)

    @property
    def cursor(self) -> str | None:
        """Cursor of the last event whose callbacks completed, or the Session's position when the connection attached if none has arrived since."""
        return self._cursor

    @property
    def generation(self) -> int | None:
        """The Session's attachment generation from the latest attach."""
        return self._generation

    async def close(self) -> None:
        """Release the Session.

        Events stop, and calls published later go to no one until another
        connection attaches. Calls already running finish and their results
        are still submitted.
        """
        if self._ended is not None:
            return
        self._end(SessionConnectionEnd("closed"))
        await self._pool.detach(self._attachment)

    async def send(
        self,
        text: str | Sequence[str],
        *,
        client_event_id: str | None = None,
        user_file_ids: Omittable[Sequence[str]] = OMIT,
        clarification_source_turn_id: Omittable[str] = OMIT,
        billing_tag: Omittable[BillingTag | None] = OMIT,
        on_progress: Callable[[str], Awaitable[None] | None] | None = None,
        timeout: float | None = None,
    ) -> TurnResult:
        """Send a user message over the connection and wait for the Turn it starts, or the active Turn it steers.

        ``on_progress`` receives the assistant's complete draft so far each
        time it grows (replace what you showed, do not append). The result is
        read once the Turn finished, with the same shape and errors as
        ``session.turn()``. ``client_event_id`` (default: a random UUID) is the
        message's stable identity: persist it before sending to recover the
        result with ``session.wait_for_result()``. A message whose answer was
        lost to a reconnect is sent again with the same ``client_event_id``,
        which the core deduplicates. ``timeout`` (default 60 seconds) is a
        local deadline that never cancels the Turn.
        """
        return await send_over_connection(
            _Channel(self),
            text,
            client_event_id=client_event_id,
            user_file_ids=user_file_ids,
            clarification_source_turn_id=clarification_source_turn_id,
            billing_tag=billing_tag,
            on_progress=on_progress,
            timeout=timeout,
        )

    async def realtime_grant(
        self,
        *,
        audio: Omittable[RealtimeOpenAudio] = OMIT,
        metadata: Omittable[Mapping[str, str]] = OMIT,
    ) -> RealtimeConnectGrant:
        """Prepare the Session's Realtime connection and return what a device needs to join it directly.

        That is the WebSocket URL and a short-lived connect token. When a
        Realtime connection is already live, a new token for it is returned
        instead and ``audio`` is not applied.
        """
        body: dict[str, Any] = {}
        if audio is not OMIT:
            body["audio"] = require_known_keys(audio, ("input", "output"), "audio")
        if metadata is not OMIT:
            if not isinstance(metadata, Mapping):
                raise TypeError("metadata must be a mapping")
            body["metadata"] = dict(metadata)
        path = f"/sessions/{segment(self.session_id)}/realtime"
        try:
            result = await self._http.post_response(path, body, outcome_unknown=True)
            opened = require_open_realtime_connection_receipt(result.data, result.request_id)
            return {
                "websocketUrl": opened["websocketUrl"],
                "connectToken": opened["connectToken"],
                "connectTokenExpiresAt": opened["connectTokenExpiresAt"],
            }
        except TiDBLinkApiError as error:
            if error.code != "realtime_connection_live":
                raise
        result = await self._http.post_response(f"{path}/connect-tokens", outcome_unknown=True)
        return require_realtime_connect_grant_receipt(result.data, result.request_id)

    # Internals ---------------------------------------------------------------

    async def _attach(self) -> None:
        await self._pool.attach(self._attachment)

    def _start(self) -> None:
        """Start delivering; the first delivery runs after the caller of ``connect()`` holds the connection."""
        self._worker = self._pool.spawn(self._deliveries())

    async def _deliveries(self) -> None:
        while True:
            job = await self._jobs.get()
            if job is _STOP:
                return
            try:
                await job()
            except Exception as error:  # pragma: no cover - deliveries report their own errors
                self._report(error)

    def _enqueue(self, job: Callable[[], Awaitable[None]]) -> None:
        self._jobs.put_nowait(job)

    def _report(self, error: BaseException) -> None:
        if self._on_error is not None:
            try:
                self._on_error(error)
                return
            except Exception as thrown:
                error = thrown
        report_unhandled(error, "Unhandled error in a TiDB Link Session connection")

    def _attach_params(self) -> dict[str, Any]:
        params: dict[str, Any] = {"userId": self.user_id, "sessionId": self.session_id}
        if self._observe:
            params["kind"] = "observing"
        if self._declarations is not None:
            params["tools"] = self._declarations
        if self._cursor is not None:
            params["cursor"] = self._cursor
        return params

    def _attached(self, result: dict[str, Any], link: BackendLink) -> None:
        self._link = link
        self._generation = result["generation"]
        # Without a resume point, delivery starts after the attach position.
        if self._cursor is None:
            self._cursor = result["cursor"]
        # Every attach replays after `_cursor`, including what was dropped.
        self._behind = False
        self._wake_link_waiters()

    def _wake_link_waiters(self) -> None:
        waiters = self._link_waiters
        self._link_waiters = []
        for waiter in waiters:
            if not waiter.done():
                waiter.set_result(None)

    async def _next_link(self, after: BackendLink | None = None) -> BackendLink:
        """The open link holding the attachment, other than ``after``; waits for a re-attach."""
        loop = asyncio.get_running_loop()
        while True:
            if self._ended is not None:
                raise TiDBLinkError(
                    f"The Session connection ended ({self._ended.reason}); connect again to send"
                )
            link = self._link
            if link is not None and link.open and link is not after:
                return link
            waiter: asyncio.Future[None] = loop.create_future()
            self._link_waiters.append(waiter)
            # A link that closed without a re-attach yet: look again shortly.
            retry = (
                loop.call_later(0.1, self._wake_link_waiters)
                if link is not None and not link.open
                else None
            )
            try:
                await waiter
            finally:
                if retry is not None:
                    retry.cancel()
                if waiter in self._link_waiters:
                    self._link_waiters.remove(waiter)

    def _watch(self) -> _EventWatch:
        watcher = _Watcher()
        self._watchers.add(watcher)
        return _EventWatch(self, watcher)

    def _notify(self, method: str, params: dict[str, Any], link: BackendLink, size: int) -> None:
        if self._ended is not None:
            return
        if method == "tool.call":
            if self._executor is not None:
                self._executor.receive(params, link)
            return
        if method != "session.event":
            return
        # Dropped events come again with the replay; check them then.
        if self._behind:
            return
        try:
            event = session_event_from(params.get("event"), params.get("cursor"), session_id=self.session_id)
        except TiDBLinkError as error:

            async def report(error: BaseException = error) -> None:
                self._report(error)

            self._enqueue(report)
            return
        if self._buffered > 0 and self._buffered + size > self._max_buffered:
            # The callbacks fell behind: drop events until they catch up, then
            # have the core send the rest again.
            self._behind = True
            self._enqueue(self._catch_up)
            return
        self._buffered += size

        async def deliver() -> None:
            # One event at a time, so that callbacks and saved cursors follow the Session's order.
            try:
                await self._deliver(event)
            finally:
                self._buffered -= size

        self._enqueue(deliver)

    async def _catch_up(self) -> None:
        if self._ended is not None or not self._behind:
            return
        await self._pool.resume(self._attachment)

    async def _deliver(self, event: SessionEvent) -> None:
        # A re-attach replays from the last completed event; skip what already ran.
        if self._ended is not None or _not_after(event["cursor"], self._cursor):
            return
        await self._run(self._on_event, event)
        known = as_known_session_event(event)
        if known is not None and known["type"] == "agent.session.realtime.input_transcript.completed":
            transcript: RealtimeTranscript = known["data"]  # type: ignore[assignment]
            await self._run(self._on_transcript, transcript)
        if known is not None and known["type"] == "agent.session.realtime.response.completed":
            response: RealtimeResponse = known["data"]  # type: ignore[assignment]
            await self._run(self._on_response, response)
        if (
            known is not None
            and known["type"] == "agent.session.turn.item.added"
            and known["data"]["event"] == "operation_step"
            and "file" in known["data"]["payload"]
        ):
            file: NotifiedFile = known["data"]["payload"]["file"]  # type: ignore[typeddict-item]
            await self._run(self._on_file, file)
        self._cursor = event["cursor"]
        for watcher in self._watchers:
            watcher.events.append(event)
            watcher.wake()
        await self._run(self._on_cursor, event["cursor"])

    async def _run(self, callback: Callback | None, value: Any) -> None:
        if callback is None:
            return
        try:
            result = callback(value)
            if inspect.isawaitable(result):
                await result
        except Exception as error:
            self._report(error)

    def _end(self, end: SessionConnectionEnd) -> None:
        if self._ended is not None:
            return
        self._ended = end
        self._link = None
        if not self._closed.done():
            self._closed.set_result(end)
        self._wake_link_waiters()
        for watcher in self._watchers:
            watcher.wake()
        # Deliveries still queued are skipped; the worker stops after them.
        self._jobs.put_nowait(_STOP)


def _handler_of(name: str, handler: object, kind: str = "Client Tool") -> ClientToolHandler:
    if isinstance(handler, ClientToolHandler):
        if not callable(handler.run):
            raise TypeError(f"{kind} {name} requires a run function")
        return handler
    if not isinstance(handler, Mapping):
        raise TypeError(f"{kind} {name} must be a ClientToolHandler or a mapping")
    fields = require_known_keys(handler, ("description", "parameters", "run"), f"{kind} {name}")
    if not callable(fields.get("run")):
        raise TypeError(f"{kind} {name} requires a run function")
    return ClientToolHandler(
        description=fields.get("description"),  # type: ignore[arg-type]
        parameters=fields.get("parameters"),  # type: ignore[arg-type]
        run=fields["run"],
    )


def connect_declarations(
    *,
    tools: Mapping[str, ClientToolHandler] | None,
    observe: object,
    cursor: object,
    max_buffered_bytes: object,
    callbacks: Mapping[str, object],
) -> tuple[dict[str, ClientToolHandler] | None, list[ClientToolDeclaration] | None]:
    """Check ``connect()`` options and turn tool handlers into declarations."""
    for key, callback in callbacks.items():
        if callback is not None and not callable(callback):
            raise TypeError(f"{key} must be callable")
    if not isinstance(observe, bool):
        raise TypeError("observe must be a boolean")
    if max_buffered_bytes is not None:
        if isinstance(max_buffered_bytes, bool) or not isinstance(max_buffered_bytes, int):
            raise TypeError("max_buffered_bytes must be a positive integer")
        if max_buffered_bytes < 1:
            raise ValueError("max_buffered_bytes must be a positive integer")
        if not observe:
            raise ValueError("max_buffered_bytes applies to observing connections only")
    if cursor is not None:
        if not isinstance(cursor, str):
            raise TypeError("cursor must be an event cursor received from this Session")
        if not _CURSOR.fullmatch(cursor):
            raise ValueError("cursor must be an event cursor received from this Session")
    if tools is None:
        return None, None
    if observe:
        raise ValueError("An observing connection cannot declare Client Tools")
    if not isinstance(tools, Mapping):
        raise TypeError("tools must map each Client Tool name to its handler")
    handlers: dict[str, ClientToolHandler] = {}
    declarations: list[ClientToolDeclaration] = []
    for name, value in tools.items():
        if not isinstance(name, str) or not _TOOL_NAME.fullmatch(name):
            raise ValueError(f"Client Tool name {name!r} must be 1-64 letters, digits, _ or -")
        handler = _handler_of(name, value)
        handlers[name] = handler
        parameters = handler.parameters
        declarations.append(
            {
                "type": "function",
                "name": name,
                "description": handler.description,
                "parameters": dict(parameters) if isinstance(parameters, Mapping) else parameters,  # type: ignore[typeddict-item]
            }
        )
    validate_client_tools(declarations)
    return handlers, declarations


async def connect_session(
    http: HttpClient,
    session_id: str,
    *,
    tools: Mapping[str, ClientToolHandler] | None = None,
    observe: bool = False,
    cursor: str | None = None,
    on_cursor: Callable[[str], Awaitable[None] | None] | None = None,
    on_event: Callable[[SessionEvent], Awaitable[None] | None] | None = None,
    on_transcript: Callable[[RealtimeTranscript], Awaitable[None] | None] | None = None,
    on_response: Callable[[RealtimeResponse], Awaitable[None] | None] | None = None,
    on_file: Callable[[NotifiedFile], Awaitable[None] | None] | None = None,
    max_buffered_bytes: int | None = None,
    on_error: Callable[[BaseException], None] | None = None,
) -> SessionConnection:
    """Open a Session connection; returns once the first attach took effect.

    ``on_cursor`` runs after each event's callbacks complete, with its cursor;
    persist it and pass it back as ``cursor`` to resume after that event.
    ``on_file`` receives each file the Agent announces with a File
    Notification, from the succeeded ``operation_step`` that carries it; read
    it with ``session.files.read(file["path"], version=file["version"])``.
    ``max_buffered_bytes`` (observing connections only, default 8 MiB) bounds
    the event data, in bytes of the frames that carried it, that may wait for
    the callbacks; past it the connection stops taking events and, once the
    callbacks have caught up, attaches again from the last event they
    completed. ``on_error`` receives errors that do not end the connection: a
    callback that raised, an invalid event, a tool result the core did not
    accept. Without it they go to the event loop's exception handler.
    """
    pool = http.backend
    user_id = http.user_id
    if pool is None or not user_id:
        raise TiDBLinkError("connect() needs a Session selected through OrganizationClient.user(user_id)")
    handlers, declarations = connect_declarations(
        tools=tools,
        observe=observe,
        cursor=cursor,
        max_buffered_bytes=max_buffered_bytes,
        callbacks={
            "on_cursor": on_cursor,
            "on_event": on_event,
            "on_transcript": on_transcript,
            "on_response": on_response,
            "on_file": on_file,
            "on_error": on_error,
        },
    )
    connection = SessionConnection(
        pool,
        http,
        user_id,
        session_id,
        handlers=handlers,
        declarations=declarations,
        observe=observe,
        cursor=cursor,
        on_cursor=on_cursor,
        on_event=on_event,
        on_transcript=on_transcript,
        on_response=on_response,
        on_file=on_file,
        max_buffered_bytes=max_buffered_bytes,
        on_error=on_error,
    )
    await connection._attach()
    connection._start()
    return connection
