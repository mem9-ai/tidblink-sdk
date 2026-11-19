"""Reads of Organization Connectors (``connectors.ts`` in the TypeScript SDK).

Serving a Connector with ``OrganizationClient.serve_connector()`` creates it;
there is no separate registration. The SDK returns the service's values
unchanged.
"""

from __future__ import annotations

from typing import TypedDict

from ._http import HttpClient, segment

__all__ = ["Connectors", "Connector"]


class Connector(TypedDict):
    """An Organization's Connector, declared by ``serve_connector`` on a Backend Connection.

    Sessions call its functions as ``mcp_<name>__<function>``.
    """

    connectorId: str
    #: The Connector's name, as ``serve_connector`` declared it.
    displayName: str
    createdAt: str
    updatedAt: str


def _connector_path(connector_id: str) -> str:
    return f"/connectors/{segment(connector_id)}"


class Connectors:
    """Reads the Organization's Connectors.

    Every Connector declared with ``OrganizationClient.serve_connector()``,
    whether or not a process serves it now. Serving a Connector creates it;
    there is no separate registration.
    """

    def __init__(self, http: HttpClient) -> None:
        self._http = http

    async def list(self, *, request_id: str | None = None) -> list[Connector]:
        response = await self._http.get("/connectors", request_id=request_id)
        return response["connectors"]  # type: ignore[no-any-return]

    async def get(self, connector_id: str, *, request_id: str | None = None) -> Connector:
        return await self._http.get(_connector_path(connector_id), request_id=request_id)
