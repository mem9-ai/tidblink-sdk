"""Reads of Organization Connectors (``connectors.ts`` in the TypeScript SDK).

Connectors are moving onto the Backend Connection, where serving one
registers it, so this SDK reads Connectors but does not register or change
them. The SDK returns the service's values unchanged.
"""

from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict

from ._http import HttpClient, segment

__all__ = ["Connectors", "Connector", "ConnectorTransport", "ConnectorObservation"]

ConnectorTransport = Literal["streamable_http", "tunnel"]

class ConnectorObservation(TypedDict):
    """What the service discovered at the endpoint."""

    era: Literal["modern", "legacy"]
    protocolVersion: str
    endpointVersion: int
    probedAt: str
    expiresAt: str


class Connector(TypedDict):
    """An Organization-owned MCP tool server that Agents can call."""

    connectorId: str
    organizationId: str
    displayName: str
    #: The HTTPS MCP endpoint of a ``streamable_http`` Connector.
    endpointUrl: str
    endpointVersion: int
    transport: ConnectorTransport
    status: Literal["inactive", "active"]
    connectorVersion: int
    createdAt: str
    updatedAt: str
    observation: NotRequired[ConnectorObservation]


def _connector_path(connector_id: str) -> str:
    return f"/connectors/{segment(connector_id)}"


class Connectors:
    """Organization Connectors: MCP tool servers that Sessions call."""

    def __init__(self, http: HttpClient) -> None:
        self._http = http

    async def list(self, *, request_id: str | None = None) -> list[Connector]:
        response = await self._http.get("/connectors", request_id=request_id)
        return response["connectors"]  # type: ignore[no-any-return]

    async def get(self, connector_id: str, *, request_id: str | None = None) -> Connector:
        return await self._http.get(_connector_path(connector_id), request_id=request_id)
