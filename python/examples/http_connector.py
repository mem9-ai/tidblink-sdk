"""A minimal HTTP Connector: an MCP server over Streamable HTTP with plain JSON
responses, without an MCP library or web framework.

Every request must carry a platform-signed token for this Organization and
Connector; every tool call carries the Call Context naming the End User it is
for. Agents see each tool as ``mcp_<name>__<tool>``, both parts lowercased
with other characters replaced by ``_``.

Run it with ``AGENT_SERVICE_URL``, ``ORGANIZATION_ID`` and ``CONNECTOR_ID``
set (``PORT`` is optional, default 8080), behind your HTTPS terminator, and
register its HTTPS URL as the Connector's ``endpoint_url``.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any

from tidblink_server import (
    ConnectorAuthError,
    ConnectorCallContext,
    ConnectorKeySet,
    read_connector_call_context,
    verify_connector_callback,
)

#: The largest request body the server reads.
MAX_BODY = 1024 * 1024

JsonBody = dict[str, Any] | None
#: Answers one request: (method, lowercased headers, body) -> (status, JSON body or None).
ConnectorHandler = Callable[[str, Mapping[str, str], bytes], Awaitable[tuple[int, JsonBody]]]


@dataclass(frozen=True)
class ConnectorTool:
    """One tool. ``run(arguments, context)`` returns text, sync or async;
    deduplicate its side effects by ``context.call_id``."""

    description: str
    input_schema: dict[str, Any]
    run: Callable[[dict[str, Any], ConnectorCallContext], Any]


def create_connector_handler(
    *,
    base_url: str,
    organization_id: str,
    connector_id: str,
    tools: Mapping[str, ConnectorTool],
    name: str = "example-connector",
    keys: ConnectorKeySet | None = None,
) -> ConnectorHandler:
    """The MCP request handler; serve it with :func:`serve_connector`."""
    keys = keys if keys is not None else ConnectorKeySet(base_url)
    catalog = [
        {"name": tool_name, "description": tool.description, "inputSchema": tool.input_schema}
        for tool_name, tool in tools.items()
    ]

    async def handle(method: str, headers: Mapping[str, str], body: bytes) -> tuple[int, JsonBody]:
        try:
            await verify_connector_callback(
                headers.get("authorization"),
                keys=keys,
                organization_id=organization_id,
                connector_id=connector_id,
            )
        except ConnectorAuthError:
            return 401, None
        except Exception:  # The verification keys could not be fetched.
            return 503, None
        if method == "DELETE":
            return 200, None
        if method != "POST":
            return 405, None
        try:
            message = json.loads(body)
        except ValueError:
            return 400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        if not isinstance(message, dict):
            return 400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
        # Notifications such as notifications/initialized need no answer.
        if "id" not in message:
            return 202, None

        def reply(result: dict[str, Any]) -> tuple[int, JsonBody]:
            return 200, {"jsonrpc": "2.0", "id": message["id"], "result": result}

        def fail(code: int, text: str) -> tuple[int, JsonBody]:
            return 200, {"jsonrpc": "2.0", "id": message["id"], "error": {"code": code, "message": text}}

        params = message.get("params")
        params = params if isinstance(params, dict) else {}
        match message.get("method"):
            case "initialize":
                return reply(
                    {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": name, "version": "1.0.0"},
                    }
                )
            case "ping":
                return reply({})
            case "tools/list":
                return reply({"tools": catalog})
            case "tools/call":
                tool_name = params.get("name")
                tool = tools.get(tool_name) if isinstance(tool_name, str) else None
                if tool is None:
                    return fail(-32602, "Unknown tool")
                try:
                    context = read_connector_call_context(params.get("_meta"), organization_id=organization_id)
                except ConnectorAuthError as error:
                    return fail(-32602, str(error))
                try:
                    text = tool.run(params.get("arguments") or {}, context)
                    if inspect.isawaitable(text):
                        text = await text
                    return reply({"content": [{"type": "text", "text": str(text)}]})
                except Exception as error:
                    return reply({"content": [{"type": "text", "text": str(error) or "Tool failed"}], "isError": True})
            case _:
                return fail(-32601, "Method not found")

    return handle


class _BadRequest(Exception):
    def __init__(self, status: int) -> None:
        self.status = status


async def _read_body(reader: asyncio.StreamReader, headers: Mapping[str, str]) -> bytes:
    if headers.get("transfer-encoding", "").lower() == "chunked":
        chunks: list[bytes] = []
        size = 0
        while True:
            line = await reader.readuntil(b"\r\n")
            try:
                length = int(line.split(b";", 1)[0].strip(), 16)
            except ValueError:
                raise _BadRequest(400) from None
            if length == 0:
                while await reader.readuntil(b"\r\n") != b"\r\n":  # Trailers
                    pass
                return b"".join(chunks)
            size += length
            if size > MAX_BODY:
                raise _BadRequest(413)
            chunks.append(await reader.readexactly(length))
            await reader.readexactly(2)
    if "transfer-encoding" in headers:
        raise _BadRequest(501)
    try:
        length = int(headers.get("content-length", "0"))
    except ValueError:
        raise _BadRequest(400) from None
    if length < 0:
        raise _BadRequest(400)
    if length > MAX_BODY:
        raise _BadRequest(413)
    return await reader.readexactly(length)


def _response(status: int, body: JsonBody, keep_alive: bool) -> bytes:
    payload = b"" if body is None else json.dumps(body).encode()
    lines = [f"HTTP/1.1 {status} {HTTPStatus(status).phrase}", f"content-length: {len(payload)}"]
    if body is not None:
        lines.append("content-type: application/json")
    lines.append(f"connection: {'keep-alive' if keep_alive else 'close'}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + payload


async def _serve_connection(
    handler: ConnectorHandler, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    try:
        while True:
            try:
                head = await reader.readuntil(b"\r\n\r\n")
            except asyncio.IncompleteReadError:
                return  # The client closed the connection.
            except asyncio.LimitOverrunError:
                writer.write(_response(431, None, False))
                return
            request_line, *header_lines = head[:-4].decode("latin-1").split("\r\n")
            parts = request_line.split(" ")
            if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
                writer.write(_response(400, None, False))
                return
            method, _target, version = parts
            headers: dict[str, str] = {}
            for line in header_lines:
                field, separator, value = line.partition(":")
                if not separator:
                    writer.write(_response(400, None, False))
                    return
                key = field.strip().lower()
                headers[key] = f"{headers[key]}, {value.strip()}" if key in headers else value.strip()
            keep_alive = version == "HTTP/1.1" and headers.get("connection", "").lower() != "close"
            try:
                body = await _read_body(reader, headers)
            except _BadRequest as refused:
                writer.write(_response(refused.status, None, False))
                return
            try:
                status, answer = await handler(method, headers, body)
            except Exception:
                status, answer = 500, None
            writer.write(_response(status, answer, keep_alive))
            await writer.drain()
            if not keep_alive:
                return
    except (ConnectionError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        pass
    finally:
        with contextlib.suppress(Exception):
            await writer.drain()
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


async def serve_connector(handler: ConnectorHandler, host: str | None = None, port: int = 8080) -> asyncio.Server:
    """Serve ``handler`` over HTTP/1.1; close the returned server when done."""
    return await asyncio.start_server(
        lambda reader, writer: _serve_connection(handler, reader, writer), host, port
    )


async def main() -> None:
    base_url = os.environ.get("AGENT_SERVICE_URL")
    organization_id = os.environ.get("ORGANIZATION_ID")
    connector_id = os.environ.get("CONNECTOR_ID")
    if not base_url or not organization_id or not connector_id:
        raise SystemExit("Set AGENT_SERVICE_URL, ORGANIZATION_ID and CONNECTOR_ID; PORT is optional")

    def lookup_order(arguments: dict[str, Any], context: ConnectorCallContext) -> str:
        return json.dumps({"orderId": arguments.get("orderId"), "userId": context.user_id, "status": "shipped"})

    handler = create_connector_handler(
        base_url=base_url,
        organization_id=organization_id,
        connector_id=connector_id,
        tools={
            "lookup_order": ConnectorTool(
                description="Look up one of the user's orders",
                input_schema={
                    "type": "object",
                    "properties": {"orderId": {"type": "string"}},
                    "required": ["orderId"],
                },
                run=lookup_order,
            )
        },
    )
    # Serve behind your HTTPS terminator; register its HTTPS URL as endpoint_url.
    server = await serve_connector(handler, port=int(os.environ.get("PORT", "8080")))
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
