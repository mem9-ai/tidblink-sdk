"""Messages sent over a Session connection (``session-send.ts`` in the TypeScript SDK)."""

from __future__ import annotations

import asyncio
import inspect
import uuid
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING, Any, Protocol

from ._backend_connection import silence
from ._http import HttpClient, require_creation_key
from ._types import OMIT, Omittable
from .errors import (
    BackendConnectionClosedError,
    BackendRequestError,
    InputNotAppliedError,
    OutcomeUnknownError,
    TiDBLinkError,
    TurnWaitTimeoutError,
)
from .history import BillingTag, SessionInteraction, require_interaction
from .turn import TurnResult, message_data, validate_wait_options, wait_for_result

if TYPE_CHECKING:
    from ._backend_pool import BackendLink
    from .events import SessionEvent

__all__ = ["send_over_connection"]

#: How long a finished Turn's result waits for the Session's release, and at least for its read.
RELEASE_WAIT = 5.0
#: Refusals after which the same message may go out on the next link.
_MOVE_ON = frozenset({"connection_draining", "session_not_attached"})


class EventWatch(Protocol):
    async def next(self) -> SessionEvent | None:
        """The next event, or None once the connection ended."""
        ...

    def stop(self) -> None: ...


class SendChannel(Protocol):
    """What ``send()`` needs from its Session connection."""

    @property
    def session_id(self) -> str: ...

    @property
    def http(self) -> HttpClient: ...

    #: Resolves when the connection ends.
    @property
    def closed(self) -> asyncio.Future[Any]: ...

    async def link(self, after: BackendLink | None = None) -> BackendLink:
        """The link that holds the attachment now, or the next one; raises once the connection ended."""
        ...

    def report(self, error: BaseException) -> None:
        """Report an error that does not end the call."""
        ...

    def watch(self) -> EventWatch:
        """Start buffering this connection's events, in delivery order."""
        ...


def _is_ambiguous(error: BaseException) -> bool:
    return isinstance(error, BackendConnectionClosedError) or (
        isinstance(error, BackendRequestError) and error.code == -32603
    )


async def send_over_connection(
    channel: SendChannel,
    text: str | Sequence[str],
    *,
    client_event_id: str | None = None,
    user_file_ids: Omittable[Sequence[str]] = OMIT,
    clarification_source_turn_id: Omittable[str] = OMIT,
    billing_tag: Omittable[BillingTag | None] = OMIT,
    on_progress: Callable[[str], Awaitable[None] | None] | None = None,
    timeout: float | None = None,
) -> TurnResult:
    """Submit a user message over the connection and wait for the Turn it starts or steers.

    Progress comes from the pushed events, and the result is read once the
    Turn's terminal event arrives.
    """
    if client_event_id is None:
        client_event_id = str(uuid.uuid4())
    require_creation_key(client_event_id)
    if on_progress is not None and not callable(on_progress):
        raise TypeError("on_progress must be callable")
    data = message_data(
        text,
        user_file_ids=user_file_ids,
        clarification_source_turn_id=clarification_source_turn_id,
        billing_tag=billing_tag,
    )
    limit, _ = validate_wait_options(timeout, None)
    loop = asyncio.get_running_loop()
    started_at = loop.time()
    deadline = started_at + limit
    session_id = channel.session_id
    event_id = client_event_id
    # Events of the Turn may arrive before the receipt.
    watch = channel.watch()
    receipt: SessionInteraction | None = None
    ambiguous = False
    turn_finished = False

    def expired() -> bool:
        return loop.time() >= deadline

    async def submit() -> SessionInteraction:
        nonlocal ambiguous
        link: BackendLink | None = None
        while True:
            async with asyncio.timeout_at(deadline):
                link = await channel.link(link)
            try:
                result = await _answer(
                    link.request("session.send", {"sessionId": session_id, "clientEventId": event_id, "data": data}),
                    channel.closed,
                    deadline,
                )
            except Exception as error:
                if _is_ambiguous(error):
                    # The core may have accepted it; the same clientEventId replays the receipt.
                    ambiguous = True
                    if isinstance(error, BackendRequestError):
                        # An internal error on a live connection: try the same one again shortly.
                        async with asyncio.timeout_at(deadline):
                            await asyncio.sleep(0.2)
                        link = None
                    continue
                if isinstance(error, BackendRequestError) and (error.reason or "") in _MOVE_ON:
                    continue
                raise
            interaction = result.get("interaction") if isinstance(result, dict) else None
            parsed = require_interaction(interaction, event_id, None)
            if parsed["kind"] != "message" or not parsed["message"] or parsed["message"]["role"] != "user":
                raise TiDBLinkError("session.send returned a different command receipt")
            return parsed

    async def until_finished(turn_id: str) -> None:
        """Follow the Turn until it finished and the Session released it.

        So the next message starts a new Turn instead of steering a finished one.
        """
        nonlocal turn_finished
        finished = False
        release_at: float | None = None
        while True:
            try:
                async with asyncio.timeout_at(deadline if release_at is None else min(deadline, release_at)):
                    event = await watch.next()
            except TimeoutError:
                # The result exists; only the release is still outstanding.
                if finished:
                    return
                raise
            # The connection ended; the REST read below still establishes the result.
            if event is None:
                return
            if event["type"] == "agent.session.idle":
                released = event["data"].get("turnId")
                # Released without a terminal event (kept for checkpoint recovery): the REST read decides.
                if released == turn_id or event["turnId"] == turn_id:
                    return
                continue
            if event["turnId"] != turn_id:
                continue
            payload = event["data"]
            inner = payload.get("payload")
            if (
                event["type"] == "agent.session.turn.output_text.snapshot"
                and isinstance(inner, dict)
                and isinstance(inner.get("text"), str)
                and on_progress is not None
            ):
                try:
                    value = on_progress(inner["text"])
                    if inspect.isawaitable(value):
                        await value
                except Exception as error:
                    # A failing display callback does not change the Turn.
                    channel.report(error)
            if payload.get("event") == "turn_finished" and not finished:
                finished = True
                turn_finished = True
                # The release normally follows at once; do not hold a known result for long.
                release_at = loop.time() + RELEASE_WAIT

    try:
        receipt = await submit()
        if receipt["rejection"] or receipt["mode"] == "rejected":
            raise InputNotAppliedError(session_id, event_id, receipt["turnId"])
        if receipt["result"]["status"] == "running":
            await until_finished(receipt["turnId"])
    except (InputNotAppliedError, BackendRequestError):
        raise
    except Exception as error:
        # Nothing reached the core: the error is definite.
        if receipt is None and not ambiguous:
            raise
        error_class = TurnWaitTimeoutError if expired() else OutcomeUnknownError
        raise error_class(
            "The message's outcome is unknown; recover it with session.wait_for_result(client_event_id)"
            if receipt is None
            else "Local wait stopped before the Turn finished; query or wait for the original input",
            session_id=session_id,
            input_id=event_id,
            turn_id=receipt["turnId"] if receipt is not None else None,
            cause=error,
        ) from error
    finally:
        watch.stop()
    # The terminal event arrived (or the connection ended): read the settled result.
    remaining = max(0.0, limit - (loop.time() - started_at))
    return await wait_for_result(
        channel.http,
        session_id,
        event_id,
        # A finished Turn's result is read even when the local deadline just passed.
        timeout=max(remaining, RELEASE_WAIT) if turn_finished else remaining,
        poll_interval=0.1,
    )


async def _answer(request: Awaitable[Any], closed: asyncio.Future[Any], deadline: float) -> Any:
    """The request's answer; raises when the connection ends first or the deadline passes."""
    answer = asyncio.ensure_future(request)
    try:
        timeout = max(0.0, deadline - asyncio.get_running_loop().time())
        await asyncio.wait({answer, closed}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
    except BaseException:
        silence(answer)
        raise
    if answer.done():
        return answer.result()
    silence(answer)
    if closed.done():
        # Ended while the answer was outstanding: the core may have accepted it.
        raise BackendConnectionClosedError(
            code=1000, message="The Session connection ended before the message's answer arrived"
        )
    raise TimeoutError()
