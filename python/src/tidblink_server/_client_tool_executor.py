"""Executes the Client Tool calls of an executing Session connection (``client-tool-executor.ts`` in the TypeScript SDK)."""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal

from .errors import BackendConnectionClosedError, BackendRequestError, TiDBLinkError
from ._http import utf16_length

if TYPE_CHECKING:
    from ._backend_pool import BackendConnectionPool, BackendLink

__all__ = ["ClientToolCallContext", "ClientToolResultRefusedError", "ClientToolExecutor"]

OUTPUT_MAX = 65_536
ERROR_MAX = 4_096
#: How long a settled callId is remembered to ignore late duplicates, in seconds.
SETTLED_MEMORY = 60.0

CallState = Literal["claiming", "running", "settled"]


class ClientToolResultRefusedError(TiDBLinkError):
    """Raised through ``on_error`` when the core did not accept a Client Tool result.

    ``reason`` is the core's: ``call_settled`` (the call already had an
    outcome, for example its deadline passed), ``not_claimed`` or
    ``call_not_found``.
    """

    def __init__(self, call_id: str, reason: str) -> None:
        super().__init__(f"The core did not accept the result of Client Tool call {call_id}: {reason}")
        self.call_id = call_id
        self.reason = reason


@dataclass(frozen=True)
class ClientToolCallContext:
    """What a Client Tool handler learns about the call it runs.

    At ``deadline_at`` the SDK cancels the handler's task and reports the
    timeout to the core, which then reports the effect as unknown.
    """

    call_id: str
    session_id: str
    user_id: str
    turn_id: str
    #: The tool's name.
    name: str
    #: Submit the result before this time.
    deadline_at: datetime


@dataclass(frozen=True)
class _ToolCall:
    call_id: str
    session_id: str
    user_id: str
    turn_id: str
    name: str
    arguments: dict[str, Any]
    deadline_at: datetime

    @property
    def deadline(self) -> float:
        """The deadline as a ``time.time()`` timestamp."""
        return self.deadline_at.timestamp()


def _parse_date(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00") if value.endswith("Z") else value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _parse_call(params: Mapping[str, Any]) -> _ToolCall | None:
    def text(name: str) -> bool:
        return isinstance(params.get(name), str) and params[name] != ""

    if not all(text(name) for name in ("callId", "sessionId", "userId", "turnId", "name", "claimBy", "deadlineAt")):
        return None
    if not isinstance(params.get("arguments"), dict):
        return None
    deadline_at = _parse_date(params["deadlineAt"])
    if deadline_at is None:
        return None
    return _ToolCall(
        call_id=params["callId"],
        session_id=params["sessionId"],
        user_id=params["userId"],
        turn_id=params["turnId"],
        name=params["name"],
        arguments=params["arguments"],
        deadline_at=deadline_at.astimezone(timezone.utc),
    )


def _truncate(text: str, limit: int) -> str:
    """At most ``limit`` UTF-16 code units, as the core counts them, ending with an ellipsis when cut."""
    if utf16_length(text) <= limit:
        return text
    units = text.encode("utf-16-le")[: 2 * (limit - 1)]
    # A pair cut in half is dropped rather than sent as a lone surrogate.
    return f"{units.decode('utf-16-le', errors='ignore')}…"


def _output_of(value: Any) -> dict[str, Any]:
    """The result the model receives for a handler's return value."""
    if isinstance(value, str):
        output = value
    elif value is None:
        output = ""
    else:
        try:
            output = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError) as cause:
            return {"error": {"message": f"The tool returned a value that is not JSON: {cause}"}}
    if utf16_length(output) > OUTPUT_MAX:
        return {"error": {"message": f"The tool output exceeds {OUTPUT_MAX} characters"}}
    return {"output": output}


def _error_of(error: BaseException) -> dict[str, Any]:
    return {"error": {"message": _truncate(str(error) or "The tool failed", ERROR_MAX)}}


class ClientToolExecutor:
    """Executes the Client Tool calls an executing Session connection receives.

    Claim first, run only after ``claimed: true``, then submit the result over
    any Backend Connection until the call's deadline. Each callId runs at most
    once.
    """

    def __init__(
        self,
        *,
        pool: BackendConnectionPool,
        session_id: str,
        handlers: Mapping[str, Any],
        report: Callable[[BaseException], None],
    ) -> None:
        self._pool = pool
        self._session_id = session_id
        #: Normalized handlers: name -> object with ``run``.
        self._handlers = handlers
        self._report = report
        self._calls: dict[str, CallState] = {}

    def receive(self, params: Mapping[str, Any], link: BackendLink) -> None:
        """Handle one ``tool.call`` from the connection that delivered it."""
        call = _parse_call(params)
        if call is None or call.session_id != self._session_id:
            self._report(TiDBLinkError("Received an invalid Client Tool call"))
            return
        # The core may send a call again after a reconnect; it runs once.
        if call.call_id in self._calls:
            return
        # Never sent: the core sends it again to the next attachment while unclaimed.
        if not link.open:
            return
        self._calls[call.call_id] = "claiming"
        self._pool.spawn(self._claim_and_run(call, link))

    async def _claim_and_run(self, call: _ToolCall, link: BackendLink) -> None:
        try:
            answer = await link.request("tool.claim", {"callId": call.call_id})
        except Exception as error:
            if isinstance(error, BackendConnectionClosedError) or (
                isinstance(error, BackendRequestError) and error.code == -32603
            ):
                # The claim may have been granted; the call must not run, so close it as failed.
                await self._submit(
                    call,
                    {
                        "error": {
                            "message": "The Application Server lost the claim's answer and did not execute the call"
                        }
                    },
                )
                return
            self._calls[call.call_id] = "settled"
            self._forget(call)
            self._report(error)
            return
        if not isinstance(answer, dict) or answer.get("claimed") is not True:
            if isinstance(answer, dict) and answer.get("reason") == "superseded":
                # Another generation holds the Session; this process may receive it again there.
                self._calls.pop(call.call_id, None)
            else:
                self._calls[call.call_id] = "settled"
                self._forget(call)
            return
        self._calls[call.call_id] = "running"
        await self._submit(call, await self._execute(call))

    async def _execute(self, call: _ToolCall) -> dict[str, Any]:
        handler = self._handlers.get(call.name)
        if handler is None:
            return {"error": {"message": f"Client Tool {call.name} has no handler in this Application Server"}}
        context = ClientToolCallContext(
            call_id=call.call_id,
            session_id=call.session_id,
            user_id=call.user_id,
            turn_id=call.turn_id,
            name=call.name,
            deadline_at=call.deadline_at,
        )

        async def run() -> dict[str, Any]:
            try:
                value = handler.run(call.arguments, context)
                if inspect.isawaitable(value):
                    value = await value
            except Exception as error:
                return _error_of(error)
            return _output_of(value)

        task = self._pool.spawn(run())
        try:
            done, _ = await asyncio.wait({task}, timeout=max(0.0, call.deadline - time.time()))
        except BaseException:
            task.cancel()
            raise
        if not done:
            # The deadline passed: stop the handler and report the timeout.
            task.cancel()
            return {"error": {"message": "The tool did not finish before its deadline"}}
        if task.cancelled():
            return {"error": {"message": "The tool was cancelled"}}
        return task.result()

    async def _submit(self, call: _ToolCall, result: dict[str, Any]) -> None:
        try:
            answer = await self._pool.request(
                "tool.result", {"callId": call.call_id, **result}, retry_until=call.deadline
            )
            if not isinstance(answer, dict) or answer.get("accepted") is not True:
                reason = answer.get("reason") if isinstance(answer, dict) else None
                if reason == "not_claimed":
                    # The lost claim never took effect: the call may arrive again and run then.
                    self._calls.pop(call.call_id, None)
                    return
                self._report(
                    ClientToolResultRefusedError(call.call_id, reason if isinstance(reason, str) else "unknown")
                )
        except Exception as error:
            self._report(error)
        self._calls[call.call_id] = "settled"
        self._forget(call)

    def _forget(self, call: _ToolCall) -> None:
        delay = max(0.0, call.deadline - time.time()) + SETTLED_MEMORY
        asyncio.get_running_loop().call_later(delay, self._calls.pop, call.call_id, None)
