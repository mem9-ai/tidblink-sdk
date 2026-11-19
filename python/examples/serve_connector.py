"""Serve the Organization's ``orders`` Connector from this backend (``serve-connector.mjs`` in the TypeScript SDK).

A cloud capability served from this backend: the Organization's ``orders``
Connector, whose functions any Session calls when its Template references the
Connector. Calls arrive over this process's Backend Connections, so the
backend needs no public endpoint.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol

from tidblink_server import ClientToolCallContext, ClientToolHandler, OrganizationClient, ServedConnector


class Orders(Protocol):
    """Your own data access. Each method must stop when its task is cancelled, as happens at a call's deadline."""

    async def list_orders(self, user_id: str, status: str | None) -> list[dict[str, Any]]: ...

    async def get_order(self, user_id: str, order_id: str) -> dict[str, Any] | None: ...


async def serve_orders_connector(
    *,
    organization: OrganizationClient,
    orders: Orders,
    name: str = "orders",
    on_error: Callable[[BaseException], None] | None = None,
) -> ServedConnector:
    """Serve the ``orders`` Connector from ``orders``.

    Each call names the End User whose Session made it; answer only with that
    user's data. The model sees the functions as ``mcp_orders__list_orders``
    and ``mcp_orders__get_order``.

    Returns the served Connector: put ``connector_id`` in the Template's
    ``tools.mcp[].serverId``, and ``close()`` it on shutdown.
    """

    async def list_orders(args: dict[str, Any], context: ClientToolCallContext) -> str:
        return json.dumps(await orders.list_orders(context.user_id, args.get("status")))

    async def get_order(args: dict[str, Any], context: ClientToolCallContext) -> str:
        order = await orders.get_order(context.user_id, args["orderId"])
        # The model sees the message of a raised exception.
        if order is None:
            raise LookupError(f"Order {args['orderId']} was not found")
        return json.dumps(order)

    return await organization.serve_connector(
        name=name,
        tools={
            "list_orders": ClientToolHandler(
                description="List the current user's orders, optionally only those with one status",
                parameters={
                    "type": "object",
                    "properties": {"status": {"type": "string", "enum": ["open", "shipped", "delivered"]}},
                    "additionalProperties": False,
                },
                run=list_orders,
            ),
            "get_order": ClientToolHandler(
                description="Read one of the current user's orders",
                parameters={
                    "type": "object",
                    "properties": {"orderId": {"type": "string"}},
                    "required": ["orderId"],
                    "additionalProperties": False,
                },
                run=get_order,
            ),
        },
        on_error=on_error,
    )


class MemoryOrders:
    """Sample orders in memory; use your own database."""

    def __init__(self, by_user: Mapping[str, Sequence[dict[str, Any]]] | None = None) -> None:
        self._by_user = by_user or {}

    async def list_orders(self, user_id: str, status: str | None) -> list[dict[str, Any]]:
        return [order for order in self._by_user.get(user_id, ()) if not status or order["status"] == status]

    async def get_order(self, user_id: str, order_id: str) -> dict[str, Any] | None:
        return next((order for order in self._by_user.get(user_id, ()) if order["orderId"] == order_id), None)


async def main() -> int:
    organization: OrganizationClient | None = None
    try:
        organization = OrganizationClient(
            base_url=os.environ.get("AGENT_SERVICE_URL", ""),
            api_key=os.environ.get("ORGANIZATION_API_KEY", ""),
        )
        connector = await serve_orders_connector(
            organization=organization,
            orders=MemoryOrders(),
            name=os.environ.get("CONNECTOR_NAME") or "orders",
            on_error=lambda error: print(error, file=sys.stderr),
        )
        # Stop serving on SIGINT or SIGTERM: closing the client ends `closed` with reason "closed".
        loop = asyncio.get_running_loop()
        client = organization
        closing: set[asyncio.Future[None]] = set()
        for stop in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(stop, lambda: closing.add(asyncio.ensure_future(client.close())))
        print(json.dumps({"connectorId": connector.connector_id}), flush=True)
        end = await connector.closed
        if end.reason == "error" and end.error is not None:
            raise end.error
        return 0
    except Exception as error:
        print(str(error) or "Serving the Connector failed", file=sys.stderr)
        return 1
    finally:
        if organization is not None:
            await organization.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
