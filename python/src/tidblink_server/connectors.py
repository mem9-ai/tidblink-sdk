"""Reads of Organization Connectors (``connectors.ts`` in the TypeScript SDK).

Serving a Connector with ``OrganizationClient.serve_connector()`` creates it;
there is no separate registration. The SDK returns the service's values
unchanged.
"""

from __future__ import annotations

from typing import Literal, TypedDict

from ._http import HttpClient, segment

__all__ = ["Connectors", "Connector", "ConnectorTransport", "ConnectorObservation"]

#: How the platform reaches a Connector. ``backend_connection`` Connectors are
#: the ones Application Servers serve with ``serve_connector``; the others are
#: older registrations, which the SDK only reads.
ConnectorTransport = Literal["backend_connection", "streamable_http", "tunnel"]


class ConnectorObservation(TypedDict):
    """What activation discovered at a ``streamable_http`` endpoint."""

    era: Literal["modern", "legacy"]
    protocolVersion: str
    endpointVersion: int
    probedAt: str
    expiresAt: str


class Connector(TypedDict):
    """An Organization-owned Connector: functions that Sessions call as ``mcp_<name>__<function>``."""

    connectorId: str
    organizationId: str
    displayName: str
    #: The HTTPS MCP endpoint of a ``streamable_http`` Connector; empty otherwise.
    endpointUrl: str
    endpointVersion: int
    transport: ConnectorTransport
    #: A ``backend_connection`` Connector is active from its first declaration.
    status: Literal["inactive", "active"]
    connectorVersion: int
    createdAt: str
    updatedAt: str
    #: What activation discovered at a ``streamable_http`` endpoint; ``None`` otherwise.
    observation: ConnectorObservation | None


def _connector_path(connector_id: str) -> str:
    return f"/connectors/{segment(connector_id)}"


class Connectors:
    """Reads the Organization's Connectors.

    Serving a Connector with ``OrganizationClient.serve_connector()`` creates
    it; there is no separate registration.
    """

    def __init__(self, http: HttpClient) -> None:
        self._http = http

    async def list(self, *, request_id: str | None = None) -> list[Connector]:
        response = await self._http.get("/connectors", request_id=request_id)
        return response["connectors"]  # type: ignore[no-any-return]

    async def get(self, connector_id: str, *, request_id: str | None = None) -> Connector:
        return await self._http.get(_connector_path(connector_id), request_id=request_id)
