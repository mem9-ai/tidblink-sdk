"""Organization Connector registrations (``connectors.ts`` in the TypeScript SDK).

The SDK returns the service's values unchanged.
"""

from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict

from ._http import HttpClient, HttpResult, segment
from .errors import OutcomeUnknownError

__all__ = ["Connectors", "Connector", "ConnectorTransport", "ConnectorObservation"]

ConnectorTransport = Literal["streamable_http", "tunnel"]

_MAX_SAFE_INTEGER = 2**53 - 1


class ConnectorObservation(TypedDict):
    """What activation discovered at the endpoint."""

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
    #: Pass this as ``expected_connector_version`` to change the registration.
    connectorVersion: int
    createdAt: str
    updatedAt: str
    observation: NotRequired[ConnectorObservation]


def _connector_path(connector_id: str) -> str:
    return f"/connectors/{segment(connector_id)}"


def _connector_receipt(result: HttpResult) -> Connector:
    data = result.data
    if not isinstance(data, dict) or not isinstance(data.get("connectorId"), str) or not data["connectorId"]:
        raise OutcomeUnknownError(
            "Connector change returned no usable registration; read it before changing it again",
            request_id=result.request_id,
        )
    return data  # type: ignore[return-value]


def _require_version(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} requires expected_connector_version")
    if not 0 <= value <= _MAX_SAFE_INTEGER:
        raise ValueError(f"{label} requires expected_connector_version")
    return value


def _optional_text(value: object, name: str) -> None:
    if value is not None and not isinstance(value, str):
        raise TypeError(f"{name} must be text")


class Connectors:
    """Organization Connector registrations.

    Register a Connector, then activate it: activation connects to the
    endpoint and discovers its tools. Edit only an inactive Connector.
    """

    def __init__(self, http: HttpClient) -> None:
        self._http = http

    async def list(self, *, request_id: str | None = None) -> list[Connector]:
        response = await self._http.get("/connectors", request_id=request_id)
        return response["connectors"]  # type: ignore[no-any-return]

    async def get(self, connector_id: str, *, request_id: str | None = None) -> Connector:
        return await self._http.get(_connector_path(connector_id), request_id=request_id)

    async def register(
        self,
        *,
        display_name: str,
        transport: ConnectorTransport | None = None,
        endpoint_url: str | None = None,
        request_id: str | None = None,
    ) -> Connector:
        """Register an inactive Connector.

        ``display_name`` is unique within the Organization. ``transport``
        defaults to ``streamable_http``, which requires ``endpoint_url``: an
        HTTPS URL without credentials. Registration is not retried; after an
        OutcomeUnknownError, list the Connectors and match the display name.
        """
        if not isinstance(display_name, str):
            raise TypeError("Connector registration requires a display_name")
        if not display_name.strip():
            raise ValueError("Connector registration requires a display_name")
        _optional_text(transport, "transport")
        _optional_text(endpoint_url, "endpoint_url")
        body: dict[str, Any] = {"displayName": display_name}
        if transport is not None:
            body["transport"] = transport
        if endpoint_url is not None:
            body["endpointUrl"] = endpoint_url
        return _connector_receipt(
            await self._http.post_response("/connectors", body, outcome_unknown=True, request_id=request_id)
        )

    async def update(
        self,
        connector_id: str,
        *,
        expected_connector_version: int,
        display_name: str | None = None,
        endpoint_url: str | None = None,
        request_id: str | None = None,
    ) -> Connector:
        """Edit an inactive Connector.

        ``expected_connector_version`` is the ``connectorVersion`` you read; a
        changed registration is refused with ConflictError.
        """
        version = _require_version(expected_connector_version, "Connector update")
        _optional_text(display_name, "display_name")
        _optional_text(endpoint_url, "endpoint_url")
        body: dict[str, Any] = {"expectedConnectorVersion": version}
        if display_name is not None:
            body["displayName"] = display_name
        if endpoint_url is not None:
            body["endpointUrl"] = endpoint_url
        return _connector_receipt(
            await self._http.patch_response(
                _connector_path(connector_id), body, outcome_unknown=True, request_id=request_id
            )
        )

    async def activate(
        self, connector_id: str, *, expected_connector_version: int, request_id: str | None = None
    ) -> Connector:
        """Discover the endpoint's tools and activate the Connector."""
        return await self._transition(connector_id, "activate", expected_connector_version, request_id)

    async def deactivate(
        self, connector_id: str, *, expected_connector_version: int, request_id: str | None = None
    ) -> Connector:
        """Stop new calls to the Connector."""
        return await self._transition(connector_id, "deactivate", expected_connector_version, request_id)

    async def _transition(
        self,
        connector_id: str,
        action: Literal["activate", "deactivate"],
        expected_connector_version: object,
        request_id: str | None,
    ) -> Connector:
        version = _require_version(expected_connector_version, f"Connector {action}")
        return _connector_receipt(
            await self._http.post_response(
                f"{_connector_path(connector_id)}/{action}",
                {"expectedConnectorVersion": version},
                # Not retried: a repeat after a lost response would be refused as a version conflict.
                outcome_unknown=True,
                request_id=request_id,
            )
        )
