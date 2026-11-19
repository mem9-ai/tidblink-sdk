"""The Backend Connections of one OrganizationClient (``backend-pool.ts`` in the TypeScript SDK)."""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol, TypeVar

from ._backend_connection import (
    BackendTransport,
    BackendWebSocket,
    WebSocketConnect,
    backend_connection_url,
    report_unhandled,
    silence,
)
from .errors import BackendConnectionClosedError, BackendRequestError, TiDBLinkError

__all__ = [
    "BackendConnectionPool",
    "BackendLink",
    "BackendWebSocket",
    "PoolAttachment",
    "PoolConnector",
    "ServedConnectorEnd",
    "SessionConnectionEnd",
    "WebSocketConnect",
]

T = TypeVar("T")


@dataclass(frozen=True)
class SessionConnectionEnd:
    """Why a Session connection ended.

    ``reason`` is ``closed`` (``close()`` or ``OrganizationClient.close()``),
    ``superseded`` (another connection attached the Session for execution),
    ``session_deleted``, or ``error``: the connection could not be restored,
    for example after the Organization API Key was revoked; ``error`` then
    holds why.
    """

    reason: Literal["closed", "superseded", "session_deleted", "error"]
    error: BaseException | None = None


@dataclass(frozen=True)
class ServedConnectorEnd:
    """Why a served Connector stopped being served.

    ``reason`` is ``closed`` (``close()`` or ``OrganizationClient.close()``)
    or ``error``: no connection could serve it any more, for example after
    the Organization API Key was revoked; ``error`` then holds why.
    """

    reason: Literal["closed", "error"]
    error: BaseException | None = None


class BackendLink(Protocol):
    """Requests over one Backend Connection."""

    @property
    def open(self) -> bool: ...

    def request(
        self,
        method: str,
        params: Mapping[str, Any],
        on_answer: Callable[[Any], None] | None = None,
    ) -> asyncio.Future[Any]:
        """``on_answer`` sees a successful answer before any frame that followed it."""
        ...


class PoolAttachment(Protocol):
    """The pool's view of one Session Attachment. The pool calls it; a Session connection implements it."""

    @property
    def session_id(self) -> str: ...

    def attach_params(self) -> dict[str, Any]:
        """Parameters of the next ``session.attach``, recomputed for every (re)attach."""
        ...

    def attached(self, result: dict[str, Any], link: BackendLink) -> None:
        """A (re)attach took effect on ``link``, which now delivers the Session."""
        ...

    def notify(self, method: str, params: dict[str, Any], link: BackendLink, size: int) -> None:
        """A notification for this Session from the connection that holds it; ``size`` is its frame's UTF-8 size."""
        ...

    def ended(self, end: SessionConnectionEnd) -> None:
        """The pool gave the attachment up; called at most once, never after ``detach()``."""
        ...


class PoolConnector(Protocol):
    """The pool's view of one Connector this process serves.

    The pool declares it on every Backend Connection; a served Connector
    implements it.
    """

    @property
    def name(self) -> str: ...

    @property
    def serve_params(self) -> Mapping[str, Any]:
        """Parameters of every ``connector.serve``."""
        ...

    def call(self, params: dict[str, Any], link: BackendLink) -> None:
        """A ``tool.call`` for this Connector from the connection that delivered it."""
        ...

    def report(self, error: BaseException) -> None:
        """A declaration failed on one connection without ending the Connector."""
        ...

    def ended(self, end: ServedConnectorEnd) -> None:
        """The pool gave the Connector up; called at most once, never after ``withdraw()``."""
        ...


DEFAULT_IDLE_CLOSE = 5.0
#: Attempts of a first attach before its caller sees the failure.
FIRST_ATTACH_ATTEMPTS = 3
#: Attaches in flight across the pool; the rest wait their turn.
ATTACH_CONCURRENCY = 32
MAX_BACKOFF = 30.0
#: Spread of re-attaches after a lost connection, so that peers do not arrive at once.
RECOVERY_JITTER = 1.0
#: How long ``close()`` waits for detach answers before closing the sockets.
CLOSE_GRACE = 2.0


def _backoff(failures: int) -> float:
    base = min(MAX_BACKOFF, 0.25 * 2 ** min(failures, 10))
    return base / 2 + random.random() * base / 2


def _is_transient(error: BaseException) -> bool:
    """Failures a later attempt, possibly on another connection, can fix."""
    return (isinstance(error, BackendConnectionClosedError) and error.retryable) or (
        isinstance(error, BackendRequestError) and error.code == -32603
    )


def _is_serve_result(value: object) -> bool:
    return isinstance(value, dict) and isinstance(value.get("connectorId"), str) and value["connectorId"] != ""


def _is_attach_result(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    generation = value.get("generation")
    cursor = value.get("cursor")
    return (
        isinstance(generation, int)
        and not isinstance(generation, bool)
        and 0 <= generation <= 2**53 - 1
        and isinstance(cursor, str)
        and cursor != ""
    )


def _safely(callback: Callable[[], object]) -> None:
    try:
        callback()
    except Exception as error:
        report_unhandled(error)


class _Slot:
    __slots__ = ("transport", "held", "reserved", "full", "draining", "idle_timer", "serving")

    def __init__(self, transport: BackendTransport) -> None:
        self.transport = transport
        #: Attachments whose notifications this connection routes, by Session.
        self.held: dict[str, PoolAttachment] = {}
        #: Attaches in flight on this connection.
        self.reserved = 0
        #: The core answered `connection_full`.
        self.full = False
        #: The core sent `connection.drain`; nothing new goes here.
        self.draining = False
        self.idle_timer: asyncio.TimerHandle | None = None
        #: Connectors declared, or being declared, on this connection, by name.
        self.serving: dict[str, asyncio.Future[str]] = {}


class BackendConnectionPool:
    """The Backend Connections of one OrganizationClient.

    Connections open on demand and hold up to the ``maxSessions`` the core
    announces; further attachments open another connection, which the load
    balancer may place on another replica. A lost connection's attachments
    re-attach elsewhere with their latest parameters; ``connection.drain``
    moves them before the core closes the connection. Served Connectors are
    declared on every connection, so that a connection executing a Session
    also serves its Connector calls, and are declared again on each new one.
    Connections that hold nothing close after a short idle period, except the
    last one while a Connector is served. Background work runs in tasks the
    pool owns; :meth:`close` ends them.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        websocket_connect: WebSocketConnect | None = None,
        idle_close: float = DEFAULT_IDLE_CLOSE,
        open_timeout: float | None = None,
    ) -> None:
        self._url = backend_connection_url(base_url)
        self._api_key = api_key
        self._websocket_connect = websocket_connect
        self._idle_close = idle_close
        self._open_timeout = open_timeout
        #: Open connections, in opening order.
        self._slots: dict[_Slot, None] = {}
        #: Attachments the application keeps, attached or being re-attached.
        self._wanted: dict[str, PoolAttachment] = {}
        #: The connection currently routing each attachment.
        self._where: dict[PoolAttachment, _Slot] = {}
        #: Connectors this process serves, by name.
        self._connectors: dict[str, PoolConnector] = {}
        #: Connectors whose ``serve()`` has not returned: it reports their failures.
        self._pending_serve: set[PoolConnector] = set()
        self._reopening = False
        self._opening: asyncio.Future[_Slot] | None = None
        self._open_failures = 0
        self._attach_limit: asyncio.Semaphore | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        #: Transports that may still be closing, so that close() can wait for them.
        self._transports: set[BackendTransport] = set()
        self._closed = False

    @property
    def connections(self) -> int:
        """Open Backend Connections, for diagnostics and tests."""
        return len(self._slots)

    @property
    def closed(self) -> bool:
        return self._closed

    def spawn(self, coroutine: Coroutine[Any, Any, T]) -> asyncio.Task[T]:
        """Run background work that :meth:`close` cancels if it is still running."""
        task = asyncio.ensure_future(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def attach(self, attachment: PoolAttachment) -> dict[str, Any]:
        """Attach a Session and keep it attached until :meth:`detach` or until the pool gives it up through ``ended()``.

        Raises when the first attach fails; one attachment per Session is
        allowed at a time. Returns the answer: ``{"generation", "cursor"}``.
        """
        if self._closed:
            raise TiDBLinkError("This OrganizationClient is closed")
        session_id = attachment.session_id
        if session_id in self._wanted:
            raise TiDBLinkError(
                f"Session {session_id} is already connected through this OrganizationClient; "
                "reuse that connection or close it first"
            )
        self._wanted[session_id] = attachment
        try:
            return await self._attach(attachment, FIRST_ATTACH_ATTEMPTS)
        except BaseException:
            if self._wanted.get(session_id) is attachment:
                del self._wanted[session_id]
            # Cancelled after the attach took effect: release it again.
            slot = self._where.pop(attachment, None)
            if slot is not None and slot.held.get(session_id) is attachment:
                self._release(slot, session_id)
                silence(slot.transport.request("session.detach", {"sessionId": session_id}))
                self._maybe_idle(slot)
            raise

    async def detach(self, attachment: PoolAttachment) -> None:
        """Release an attachment. Best effort: a lost connection releases it anyway."""
        session_id = attachment.session_id
        if self._wanted.get(session_id) is not attachment:
            return
        del self._wanted[session_id]
        slot = self._where.pop(attachment, None)
        if slot is None or slot.held.get(session_id) is not attachment:
            return
        self._release(slot, session_id)
        try:
            await slot.transport.request("session.detach", {"sessionId": session_id})
        except Exception:
            pass  # The attachment lapses with its connection.
        finally:
            self._maybe_idle(slot)

    async def resume(self, attachment: PoolAttachment) -> None:
        """Attach again on the connection that holds the attachment, so that the core replays its events after the attachment's cursor.

        Only for observing attachments: an executing attach could take the
        Session back from a newer executor. Nothing happens while the
        attachment is moving to another connection, whose attach replays the
        same way. A transient failure is recovered like a lost connection;
        any other ends the attachment.
        """
        session_id = attachment.session_id
        if self._wanted.get(session_id) is not attachment:
            return
        slot = self._where.get(attachment)
        if slot is None or slot.held.get(session_id) is not attachment:
            return

        def holds() -> bool:
            return self._wanted.get(session_id) is attachment and slot.held.get(session_id) is attachment

        def take(answer: Any) -> None:
            # The replay may arrive with the answer: take it from the answer on.
            if _is_attach_result(answer) and holds():
                attachment.attached(answer, slot.transport)

        try:
            async with self._limit():
                # Closed or moved while waiting for its turn: nothing to resume.
                if not holds():
                    return
                result = await slot.transport.request("session.attach", attachment.attach_params(), take)
            if not _is_attach_result(result):
                raise TiDBLinkError("session.attach returned an invalid result")
        except Exception as error:
            if not holds():
                return
            self._release(slot, session_id)
            self._where.pop(attachment, None)
            silence(slot.transport.request("session.detach", {"sessionId": session_id}))
            self._maybe_idle(slot)
            reason = error.reason if isinstance(error, BackendRequestError) else None
            if (
                not isinstance(error, BackendRequestError)
                or error.code == -32603
                or reason in ("connection_full", "connection_draining")
            ):
                # Another connection, or a later attempt, may take it.
                self.spawn(self._recover(attachment, RECOVERY_JITTER))
                return
            del self._wanted[session_id]
            end = (
                SessionConnectionEnd("session_deleted")
                if reason == "session_not_found"
                else SessionConnectionEnd("error", error)
            )
            _safely(lambda: attachment.ended(end))

    async def request(
        self,
        method: str,
        params: Mapping[str, Any],
        *,
        retry_until: float | None = None,
    ) -> Any:
        """Send a request that any connection of the Organization may carry, such as ``tool.result``.

        Transient failures are retried, on another connection when the one
        used was lost, until ``retry_until`` (a ``time.time()`` timestamp).
        Only idempotent requests belong here.
        """
        until = retry_until or 0.0
        failures = 0
        while True:
            try:
                slot = await self._any_slot()
            except Exception as error:
                if not _is_transient(error) or time.time() >= until:
                    raise
                await asyncio.sleep(min(_backoff(failures), max(0.0, until - time.time())))
                failures += 1
                continue
            self._cancel_idle(slot)
            try:
                return await slot.transport.request(method, params)
            except Exception as error:
                if not _is_transient(error) or time.time() >= until:
                    raise
                if isinstance(error, BackendRequestError):
                    await asyncio.sleep(min(_backoff(failures), max(0.0, until - time.time())))
            finally:
                self._maybe_idle(slot)
            failures += 1

    async def serve(self, connector: PoolConnector) -> str:
        """Serve a Connector on every connection, now and on each one opened later.

        It is served until :meth:`withdraw` or until the pool gives it up
        through ``ended()``. Returns its connectorId once one connection
        serves it; raises when the first declaration fails. One Connector per
        name is allowed at a time.
        """
        if self._closed:
            raise TiDBLinkError("This OrganizationClient is closed")
        name = connector.name
        if name in self._connectors:
            raise TiDBLinkError(f"Connector {name} is already served through this OrganizationClient; close it first")
        self._connectors[name] = connector
        self._pending_serve.add(connector)
        try:
            failures = 0
            while True:
                if self._connectors.get(name) is not connector:
                    raise TiDBLinkError("The Connector was closed while it was being served")
                slot: _Slot | None = None
                try:
                    slot = await self._serving_slot()
                    self._cancel_idle(slot)
                    connector_id = await asyncio.shield(self._declare(slot, connector))
                    for other in list(self._slots):
                        if other is not slot and self._serves(other):
                            self._declare_quietly(other, connector)
                    return connector_id
                except Exception as error:
                    failures += 1
                    if isinstance(error, BackendRequestError) and error.reason == "connection_draining":
                        if slot is not None:
                            slot.draining = True
                    elif not _is_transient(error):
                        raise
                    if failures >= FIRST_ATTACH_ATTEMPTS:
                        raise
                    await asyncio.sleep(_backoff(failures))
                finally:
                    if slot is not None:
                        self._maybe_idle(slot)
        except BaseException:
            if self._connectors.get(name) is connector:
                # Forget it at once, so that the name can be served again; withdraw in the background.
                self.spawn(self._await_withdrawals(self._withdraw_everywhere(connector)))
            raise
        finally:
            self._pending_serve.discard(connector)

    async def withdraw(self, connector: PoolConnector) -> None:
        """Stop serving a Connector on every connection. Best effort: a lost connection ends it anyway."""
        if self._connectors.get(connector.name) is not connector:
            return
        await self._await_withdrawals(self._withdraw_everywhere(connector))

    async def close(self) -> None:
        """End every attachment and served Connector with reason ``closed``, release them and close the connections.

        Later attaches and serves are refused. Background work still running
        is cancelled.
        """
        if self._closed:
            return
        self._closed = True
        connectors = list(self._connectors.values())
        self._connectors.clear()
        for connector in connectors:
            _safely(lambda connector=connector: connector.ended(ServedConnectorEnd("closed")))
        attachments = list(self._wanted.values())
        self._wanted.clear()
        detaches: list[asyncio.Future[Any]] = []
        for attachment in attachments:
            slot = self._where.pop(attachment, None)
            if slot is not None and slot.held.get(attachment.session_id) is attachment:
                self._release(slot, attachment.session_id)
                detaches.append(
                    silence(slot.transport.request("session.detach", {"sessionId": attachment.session_id}))
                )
            _safely(lambda attachment=attachment: attachment.ended(SessionConnectionEnd("closed")))
        if detaches:
            await asyncio.wait(detaches, timeout=CLOSE_GRACE)
        for slot in list(self._slots):
            self._cancel_idle(slot)
            slot.transport.close(1000, "client closed")
        self._slots.clear()
        if self._opening is not None:
            self._opening.cancel()
        current = asyncio.current_task()
        tasks = [task for task in self._tasks if task is not current]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        transports = list(self._transports)
        if transports:
            waiting = [asyncio.ensure_future(transport.wait_closed()) for transport in transports]
            _, late = await asyncio.wait(waiting, timeout=CLOSE_GRACE + 1.0)
            for transport in transports:
                transport.abort()
            if late:
                await asyncio.gather(*late, return_exceptions=True)

    # Internals -----------------------------------------------------------------

    def _limit(self) -> asyncio.Semaphore:
        if self._attach_limit is None:
            self._attach_limit = asyncio.Semaphore(ATTACH_CONCURRENCY)
        return self._attach_limit

    async def _attach(self, attachment: PoolAttachment, attempts: float) -> dict[str, Any]:
        session_id = attachment.session_id
        failures = 0
        while True:
            if self._wanted.get(session_id) is not attachment:
                raise TiDBLinkError("The Session connection was closed while attaching")
            slot: _Slot | None = None
            try:
                slot = await self._reserve_slot()
                held = slot
                taken = False

                # The Session's events may arrive with the answer: route them from the answer on.
                def take(result: Any, held: _Slot = held) -> None:
                    nonlocal taken
                    if not _is_attach_result(result) or self._wanted.get(session_id) is not attachment:
                        return
                    taken = True
                    held.held[session_id] = attachment
                    self._where[attachment] = held
                    attachment.attached(result, held.transport)

                async with self._limit():
                    result = await held.transport.request("session.attach", attachment.attach_params(), take)
                if not _is_attach_result(result):
                    raise TiDBLinkError("session.attach returned an invalid result")
                if not taken:
                    # Closed while the attach was in flight: release what it took.
                    silence(held.transport.request("session.detach", {"sessionId": session_id}))
                    raise TiDBLinkError("The Session connection was closed while attaching")
                return result
            except Exception as error:
                failures += 1
                if (
                    isinstance(error, BackendRequestError)
                    and slot is not None
                    and error.reason in ("connection_full", "connection_draining")
                ):
                    # Try another connection at once; a core that refuses every one still ends the loop.
                    if error.reason == "connection_full":
                        slot.full = True
                    else:
                        slot.draining = True
                    if failures >= attempts:
                        raise
                    if failures > 2:
                        await asyncio.sleep(_backoff(failures))
                    continue
                if not _is_transient(error) or failures >= attempts:
                    raise
                await asyncio.sleep(_backoff(failures))
            finally:
                if slot is not None:
                    slot.reserved -= 1
                    self._maybe_idle(slot)

    async def _recover(self, attachment: PoolAttachment, jitter: float) -> None:
        """Re-attach after a lost or draining connection, until it works or is given up."""
        try:
            if jitter:
                await asyncio.sleep(random.random() * jitter)
            await self._attach(attachment, float("inf"))
        except Exception as error:
            if self._wanted.get(attachment.session_id) is not attachment:
                return
            del self._wanted[attachment.session_id]
            _safely(lambda: attachment.ended(SessionConnectionEnd("error", error)))

    async def _reserve_slot(self) -> _Slot:
        """A connection with room for one more attachment, reserved for the caller."""
        while True:
            for slot in self._slots:
                if (
                    slot.transport.open
                    and not slot.draining
                    and not slot.full
                    and len(slot.held) + slot.reserved < slot.transport.hello.max_sessions
                ):
                    # Reserve before yielding, so that concurrent attaches count each other.
                    slot.reserved += 1
                    self._cancel_idle(slot)
                    return slot
            await self._open_shared()

    def _serves(self, slot: _Slot) -> bool:
        """Whether new requests may go to this connection."""
        return slot in self._slots and slot.transport.open and not slot.draining

    async def _serving_slot(self) -> _Slot:
        """An open connection that takes new requests, opened if there is none."""
        for slot in self._slots:
            if self._serves(slot):
                return slot
        return await self._open_shared()

    def _declare(self, slot: _Slot, connector: PoolConnector) -> asyncio.Future[str]:
        """Declare a Connector on one connection; repeated calls share the declaration.

        Await it through ``asyncio.shield``: it is shared.
        """
        name = connector.name
        existing = slot.serving.get(name)
        if existing is not None:
            return existing
        declaration: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        silence(declaration)

        def settle(answer: asyncio.Future[Any]) -> None:
            if declaration.done():
                return
            error: BaseException | None
            if answer.cancelled():
                error = TiDBLinkError("connector.serve was cancelled")
            else:
                error = answer.exception()
                if error is None and not _is_serve_result(answer.result()):
                    error = TiDBLinkError("connector.serve returned an invalid result")
            if error is None:
                declaration.set_result(answer.result()["connectorId"])
                return
            if slot.serving.get(name) is declaration:
                del slot.serving[name]
            declaration.set_exception(error)

        slot.serving[name] = declaration
        slot.transport.request("connector.serve", connector.serve_params).add_done_callback(settle)
        return declaration

    def _declare_quietly(self, slot: _Slot, connector: PoolConnector) -> None:
        """Declare a Connector on a further connection.

        A transient refusal is retried there while the connection still
        serves; a lost or draining connection is replaced and declares it
        again; other failures are reported.
        """
        # Start now, so that a concurrent declaration on this connection shares it.
        self.spawn(self._settle_declaration(slot, connector, self._declare(slot, connector)))

    async def _settle_declaration(
        self, slot: _Slot, connector: PoolConnector, declaration: asyncio.Future[str]
    ) -> None:
        failures = 0
        while True:
            try:
                await asyncio.shield(declaration)
                return
            except Exception as error:
                if self._connectors.get(connector.name) is not connector:
                    return
                if isinstance(error, BackendConnectionClosedError) or (
                    isinstance(error, BackendRequestError) and error.reason == "connection_draining"
                ):
                    return
                if _is_transient(error) and self._serves(slot):
                    # The connection stays open: serve it there again later.
                    failures += 1
                    await asyncio.sleep(_backoff(failures))
                    if self._connectors.get(connector.name) is not connector or not self._serves(slot):
                        return
                    declaration = self._declare(slot, connector)
                    continue
                if connector not in self._pending_serve:
                    _safely(lambda: connector.report(error))
                return

    def _withdraw_everywhere(self, connector: PoolConnector) -> list[asyncio.Future[Any]]:
        """Stop serving a Connector now; returns the ``connector.withdraw`` requests sent."""
        name = connector.name
        if self._connectors.get(name) is connector:
            del self._connectors[name]
        withdrawals: list[asyncio.Future[Any]] = []
        for slot in self._slots:
            if slot.serving.pop(name, None) is None:
                continue
            if slot.transport.open:
                withdrawals.append(silence(slot.transport.request("connector.withdraw", {"name": name})))
        return withdrawals

    async def _await_withdrawals(self, withdrawals: list[asyncio.Future[Any]]) -> None:
        try:
            if withdrawals:
                await asyncio.wait(withdrawals)
        finally:
            for slot in list(self._slots):
                self._maybe_idle(slot)

    def _end_connectors(self, error: BaseException) -> None:
        connectors = list(self._connectors.values())
        self._connectors.clear()
        for connector in connectors:
            _safely(lambda connector=connector: connector.ended(ServedConnectorEnd("error", error)))

    async def _keep_serving(self) -> None:
        """Keep one connection open while Connectors are served.

        Reopens after the last one was lost or started draining; the new
        connection declares them.
        """
        if self._reopening:
            return
        self._reopening = True
        try:
            failures = 0
            while self._connectors and not self._closed:
                if any(self._serves(slot) for slot in self._slots):
                    return
                try:
                    await self._open_shared()
                    return
                except Exception as error:
                    if self._closed:
                        return
                    if not _is_transient(error):
                        self._end_connectors(error)
                        return
                    await asyncio.sleep(_backoff(failures))
                failures += 1
        finally:
            self._reopening = False

    async def _any_slot(self) -> _Slot:
        fallback: _Slot | None = None
        for slot in self._slots:
            if not slot.transport.open:
                continue
            if not slot.draining:
                return slot
            if fallback is None:
                fallback = slot
        return fallback if fallback is not None else await self._open_shared()

    async def _open_shared(self) -> _Slot:
        """One connection opens at a time; concurrent callers share it."""
        if self._closed:
            raise TiDBLinkError("This OrganizationClient is closed")
        if self._opening is None:
            opening = asyncio.ensure_future(self._open())
            self._opening = opening

            def opened(done: asyncio.Future[_Slot]) -> None:
                if self._opening is done:
                    self._opening = None
                if not done.cancelled():
                    done.exception()

            opening.add_done_callback(opened)
        opening = self._opening
        try:
            return await asyncio.shield(opening)
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if opening.cancelled() and self._closed and (task is None or not task.cancelling()):
                raise TiDBLinkError("This OrganizationClient is closed") from None
            raise

    async def _open(self) -> _Slot:
        if self._open_failures:
            await asyncio.sleep(_backoff(self._open_failures))
        slot: _Slot | None = None

        def on_notification(method: str, params: dict[str, Any], size: int) -> None:
            if slot is not None:
                self._notify(slot, method, params, size)

        try:
            transport = await BackendTransport.connect(
                url=self._url,
                api_key=self._api_key,
                websocket_connect=self._websocket_connect,
                open_timeout=self._open_timeout,
                on_notification=on_notification,
            )
        except Exception:
            self._open_failures += 1
            raise
        self._open_failures = 0
        self._transports.add(transport)
        transport.closed.add_done_callback(lambda _: self._forget_transport(transport))
        slot = _Slot(transport)
        if self._closed:
            transport.close(1000, "client closed")
            raise TiDBLinkError("This OrganizationClient is closed")
        self._slots[slot] = None
        opened = slot
        transport.closed.add_done_callback(lambda done: self._lost(opened, done.result()))
        for connector in list(self._connectors.values()):
            self._declare_quietly(slot, connector)
        self._maybe_idle(slot)
        return slot

    def _forget_transport(self, transport: BackendTransport) -> None:
        # Keep it until its socket closed, so that close() can wait for it.
        async def forget() -> None:
            await transport.wait_closed()
            self._transports.discard(transport)

        if not self._closed:
            self.spawn(forget())

    def _notify(self, slot: _Slot, method: str, params: dict[str, Any], size: int) -> None:
        if method == "connection.drain":
            close_after = params.get("closeAfterMs")
            finite = isinstance(close_after, (int, float)) and not isinstance(close_after, bool)
            self._drain(slot, float(close_after) / 1000 if finite else 0.0)  # type: ignore[arg-type]
            return
        # A Connector call needs no attachment: route it by the Connector it names.
        if method == "tool.call" and "connector" in params:
            reference = params["connector"]
            name = reference.get("name") if isinstance(reference, dict) else None
            connector = self._connectors.get(name) if isinstance(name, str) else None
            # Withdrawn here: unclaimed, the call ends as not performed.
            if connector is not None:
                _safely(lambda: connector.call(params, slot.transport))  # type: ignore[union-attr]
            return
        session_id = params.get("sessionId")
        if not isinstance(session_id, str):
            return
        attachment = slot.held.get(session_id)
        if attachment is None:
            return
        if method == "session.detached":
            self._release(slot, session_id)
            self._where.pop(attachment, None)
            if self._wanted.get(session_id) is attachment:
                del self._wanted[session_id]
            end = SessionConnectionEnd(
                "session_deleted" if params.get("reason") == "session_deleted" else "superseded"
            )
            _safely(lambda: attachment.ended(end))
            self._maybe_idle(slot)
            return
        _safely(lambda: attachment.notify(method, params, slot.transport, size))

    def _drain(self, slot: _Slot, close_after: float) -> None:
        """Move every attachment to another connection, then close this one."""
        if slot.draining and not slot.held:
            return
        slot.draining = True
        deadline = time.monotonic() + close_after
        moving = list(slot.held.values())
        # Stop routing now: each re-attach resumes from what its Session processed.
        slot.held.clear()
        moves = []
        for attachment in moving:
            self._where.pop(attachment, None)
            moves.append(self.spawn(self._recover(attachment, 0)))
        # Serve the Connectors on another connection before this one closes.
        if self._connectors:
            moves.append(self.spawn(self._keep_serving()))

        async def close_when_moved() -> None:
            if moves:
                await asyncio.gather(*moves, return_exceptions=True)
            # Let claims and results in flight finish, then close before the core does.
            while slot.transport.open and slot.transport.pending_requests > 0 and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            slot.transport.close(1000, "drained")

        self.spawn(close_when_moved())

    def _lost(self, slot: _Slot, error: BackendConnectionClosedError) -> None:
        self._slots.pop(slot, None)
        self._cancel_idle(slot)
        held = list(slot.held.values())
        slot.held.clear()
        for attachment in held:
            self._where.pop(attachment, None)
            if self._wanted.get(attachment.session_id) is not attachment:
                continue
            if error.retryable and not self._closed:
                self.spawn(self._recover(attachment, RECOVERY_JITTER))
            else:
                del self._wanted[attachment.session_id]
                _safely(lambda attachment=attachment: attachment.ended(SessionConnectionEnd("error", error)))
        if not self._connectors or self._closed:
            return
        if error.retryable:
            self.spawn(self._keep_serving())
        elif not any(self._serves(other) for other in self._slots):
            # The Key no longer works: no connection can serve them.
            self._end_connectors(error)

    def _release(self, slot: _Slot, session_id: str) -> None:
        slot.held.pop(session_id, None)
        slot.full = False

    def _cancel_idle(self, slot: _Slot) -> None:
        if slot.idle_timer is not None:
            slot.idle_timer.cancel()
            slot.idle_timer = None

    def _keeps_serving(self, slot: _Slot) -> bool:
        """The only connection serving the Connectors stays open while any is served."""
        return (
            bool(self._connectors)
            and self._serves(slot)
            and not any(other is not slot and self._serves(other) for other in self._slots)
        )

    def _maybe_idle(self, slot: _Slot) -> None:
        if slot.idle_timer is not None or slot not in self._slots or slot.held or slot.reserved:
            return
        if self._keeps_serving(slot):
            return

        def idle() -> None:
            slot.idle_timer = None
            if slot not in self._slots or slot.held or slot.reserved:
                return
            if self._keeps_serving(slot):
                return
            # Claims and results may still be in flight; look again later.
            if slot.transport.pending_requests:
                self._maybe_idle(slot)
                return
            del self._slots[slot]
            slot.transport.close(1000, "idle")

        slot.idle_timer = asyncio.get_running_loop().call_later(self._idle_close, idle)

