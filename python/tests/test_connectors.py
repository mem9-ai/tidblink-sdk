"""Reads of Organization Connectors."""

from __future__ import annotations

from support import json_response, serve

CONNECTOR_ID = "conn_00000000-0000-0000-0000-000000000001"
registration = {
    "connectorId": CONNECTOR_ID,
    "organizationId": "org_1",
    "displayName": "Orders",
    "endpointUrl": "https://orders.example.com/mcp",
    "endpointVersion": 1,
    "transport": "streamable_http",
    "status": "active",
    "connectorVersion": 1,
    "createdAt": "2026-11-11T00:00:00.000Z",
    "updatedAt": "2026-11-11T00:00:00.000Z",
}


async def test_connectors_are_listed_and_read_unchanged() -> None:
    def handler(request, service):
        if request.url.path.endswith("/connectors"):
            return json_response({"connectors": [registration]})
        return json_response(registration)

    organization, service = serve(handler)
    async with organization:
        assert await organization.connectors.list(request_id="list-1") == [registration]
        assert await organization.connectors.get(CONNECTOR_ID) == registration
    assert [(call.method, call.pathname) for call in service.calls] == [
        ("GET", "/api/v1/org/connectors"),
        ("GET", f"/api/v1/org/connectors/{CONNECTOR_ID}"),
    ]
    assert service.calls[0].headers["x-request-id"] == "list-1"


def test_connectors_offer_no_registration_management() -> None:
    from tidblink_server import Connectors

    for name in ("register", "update", "activate", "deactivate"):
        assert not hasattr(Connectors, name)
