"""Connectors served over this process's Backend Connections (``connector-serve.ts`` in the TypeScript SDK)."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Mapping
from typing import Any

from ._backend_connection import report_unhandled
from ._backend_pool import BackendConnectionPool, BackendLink, ServedConnectorEnd
from ._client_tool_executor import ClientToolExecutor, ToolCallConnector
from ._http import utf16_length
from .client_tools import ClientToolDeclaration, validate_client_tools
from .session_connection import ClientToolHandler, ClientToolHandlers, _handler_of

__all__ = ["ServedConnector", "ServedConnectorEnd", "ToolCallConnector", "connector_declarations", "serve_connector"]

#: The platform-reserved argument through which the model asks not to wait.
_ASYNC_ARGUMENT = "async"
_NAME = re.compile(r"[a-z0-9]+(?:_[a-z0-9]+)*")
_NAME_RULE = "lowercase letters and digits joined by single underscores, 1 to 29 characters"
MAX_FUNCTIONS = 64
DESCRIPTION_MAX = 4096


def _require_name(value: object, what: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{what} must be a string")
    if len(value) > 29 or not _NAME.fullmatch(value):
        raise ValueError(f"{what} must be {_NAME_RULE}")


def connector_declarations(
    *, name: object, tools: object, on_error: object
) -> tuple[dict[str, ClientToolHandler], list[ClientToolDeclaration]]:
    """Check ``serve_connector()`` arguments and turn its handlers into declarations."""
    _require_name(name, "The Connector name")
    if on_error is not None and not callable(on_error):
        raise TypeError("on_error must be callable")
    if not isinstance(tools, Mapping):
        raise TypeError("tools must map each Connector function name to its handler")
    if len(tools) > MAX_FUNCTIONS:
        raise ValueError(f"A Connector serves at most {MAX_FUNCTIONS} functions")
    handlers: dict[str, ClientToolHandler] = {}
    declarations: list[ClientToolDeclaration] = []
    for function, value in tools.items():
        _require_name(function, f"Connector function name {function!r}")
        handler = _handler_of(function, value, "Connector function")
        description = handler.description
        if isinstance(description, str) and utf16_length(description) > DESCRIPTION_MAX:
            raise ValueError(
                f"The description of Connector function {function} exceeds {DESCRIPTION_MAX} characters"
            )
        parameters = handler.parameters
        if isinstance(parameters, Mapping):
            if parameters.get("type") != "object":
                raise ValueError(f'The parameters of Connector function {function} must have type "object"')
            properties = parameters.get("properties")
            required = parameters.get("required")
            if (isinstance(properties, Mapping) and _ASYNC_ARGUMENT in properties) or (
                isinstance(required, list) and _ASYNC_ARGUMENT in required
            ):
                raise ValueError(
                    f"The parameters of Connector function {function} may not define or require "
                    'the platform-reserved "async" property'
                )
        handlers[function] = handler
        declarations.append(
            {
                "type": "function",
                "name": function,
                "description": description,
                "parameters": dict(parameters) if isinstance(parameters, Mapping) else parameters,  # type: ignore[typeddict-item]
            }
        )
    validate_client_tools(declarations)
    return handlers, declarations


class ServedConnector:
    """A Connector this process serves over its Backend Connections.

    Its calls can come from any Session whose Template references
    ``connector_id``, and run on the connection the core sends them to. It is
    served until :meth:`close`, or until ``closed`` reports why it ended. Use
    ``OrganizationClient.serve_connector()`` to serve one.
    """

    def __init__(
        self,
        *,
        name: str,
        connector_id: str,
        closed: asyncio.Future[ServedConnectorEnd],
        close: Callable[[], Any],
    ) -> None:
        self.name = name
        #: The Organization's Connector with this name; Templates reference it as ``tools.mcp[].serverId``.
        self.connector_id = connector_id
        self._closed = closed
        self._close = close

    def __repr__(self) -> str:
        return f"ServedConnector(name={self.name!r}, connector_id={self.connector_id!r})"

    @property
    def closed(self) -> asyncio.Future[ServedConnectorEnd]:
        """Resolves once, with why serving ended.

        Awaiting it with a timeout, or cancelling a task that awaits it, does
        not affect serving.
        """
        return asyncio.shield(self._closed)

    async def close(self) -> None:
        """Stop serving on every connection.

        Calls published later find no connection and end as not performed;
        calls already running finish and their results are still submitted.
        """
        await self._close()


class _Registration:
    """The pool's view of a served Connector."""

    __slots__ = ("name", "serve_params", "_executor", "report", "ended")

    def __init__(
        self,
        name: str,
        serve_params: dict[str, Any],
        executor: ClientToolExecutor,
        report: Callable[[BaseException], None],
        ended: Callable[[ServedConnectorEnd], None],
    ) -> None:
        self.name = name
        self.serve_params = serve_params
        self._executor = executor
        self.report = report
        self.ended = ended

    def call(self, params: dict[str, Any], link: BackendLink) -> None:
        self._executor.receive(params, link)


async def serve_connector(
    pool: BackendConnectionPool,
    *,
    name: str,
    tools: ClientToolHandlers,
    on_error: Callable[[BaseException], None] | None = None,
) -> ServedConnector:
    """Serve a Connector; returns once one connection serves it."""
    handlers, declarations = connector_declarations(name=name, tools=tools, on_error=on_error)

    def report(error: BaseException) -> None:
        if on_error is not None:
            try:
                on_error(error)
                return
            except Exception as thrown:
                error = thrown
        report_unhandled(error, "Unhandled error in a served TiDB Link Connector")

    closed: asyncio.Future[ServedConnectorEnd] = asyncio.get_running_loop().create_future()

    def end(value: ServedConnectorEnd) -> None:
        if not closed.done():
            closed.set_result(value)

    executor = ClientToolExecutor(pool=pool, connector_name=name, handlers=handlers, report=report)
    registration = _Registration(name, {"name": name, "tools": declarations}, executor, report, end)
    connector_id = await pool.serve(registration)

    async def close() -> None:
        if closed.done():
            return
        end(ServedConnectorEnd("closed"))
        await pool.withdraw(registration)

    return ServedConnector(name=name, connector_id=connector_id, closed=closed, close=close)
