"""The Organization API Key client (``organization.ts`` in the TypeScript SDK)."""

from __future__ import annotations

from typing import Literal, TypedDict

import httpx

from ._backend_pool import BackendConnectionPool, WebSocketConnect
from ._http import (
    HttpClient,
    default_http_client,
    is_organization_api_key,
    normalize_base_url,
    segment,
)
from ._types import OMIT, Omittable
from .connectors import Connectors
from .errors import OutcomeUnknownError
from .skills import Skills
from .templates import Templates
from .usage import (
    BillingUsageFactKind,
    BillingUsageSeriesInterval,
    OrganizationUserUsageDetails,
    OrganizationUserUsageSummary,
    usage_query,
    user_usage_path,
)
from .user import EndUserClient


class Organization(TypedDict):
    organizationId: str
    slug: str
    name: str | None
    createdAt: str
    updatedAt: str


class EndUser(TypedDict):
    userId: str
    organizationId: str
    displayName: str | None
    externalId: str | None
    status: Literal["active", "disabled"]
    createdAt: str
    updatedAt: str


class OrganizationClient:
    """Customer operations authorized by an Organization API Key.

    ``http_client`` replaces the transport of every HTTP request the client
    makes, including audio Turn streams and file transfers to presigned URLs
    (which never receive the API Key): use it for proxies, instrumentation or
    tests. ``websocket_connect`` replaces how Backend Connections are opened.

    Use it as an async context manager, or call :meth:`close` when done.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        http_client: httpx.AsyncClient | None = None,
        websocket_connect: WebSocketConnect | None = None,
    ) -> None:
        if not is_organization_api_key(api_key):
            # Never include the value: it may be a credential.
            raise (ValueError if isinstance(api_key, str) else TypeError)(
                "OrganizationClient requires an Organization API Key"
            )
        if http_client is not None and not isinstance(http_client, httpx.AsyncClient):
            raise TypeError("http_client must be an httpx.AsyncClient")
        if websocket_connect is not None and not callable(websocket_connect):
            raise TypeError("websocket_connect must be callable")
        normalized = normalize_base_url(base_url)
        self._owns_http_client = http_client is None
        self._backend = BackendConnectionPool(
            base_url=normalized, api_key=api_key, websocket_connect=websocket_connect
        )
        self._http = HttpClient(
            base_url=normalized,
            api_key=api_key,
            http_client=http_client if http_client is not None else default_http_client(),
            backend=self._backend,
        )
        self._templates = Templates(self._http)
        self._skills = Skills(self._http)
        self._connectors = Connectors(self._http)

    async def __aenter__(self) -> OrganizationClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    @property
    def templates(self) -> Templates:
        """Organization Agent Templates."""
        return self._templates

    @property
    def skills(self) -> Skills:
        """Organization Skills that Templates reference."""
        return self._skills

    @property
    def connectors(self) -> Connectors:
        """Organization Connectors: MCP tool servers that Sessions call."""
        return self._connectors

    async def close(self) -> None:
        """Close every Session connection opened through this client, with reason ``closed``, and its Backend Connections.

        Later ``connect()`` calls are refused. The HTTP transport the client
        created is closed too; one passed as ``http_client`` stays open.
        """
        try:
            await self._backend.close()
        finally:
            if self._owns_http_client:
                await self._http.client.aclose()

    def user(self, user_id: str) -> EndUserClient:
        """Select the End User who owns the resources; the Organization API Key remains the caller."""
        return EndUserClient(self._http.for_user(user_id))

    async def get_user_usage(
        self,
        user_id: str,
        *,
        from_: str,
        to: str,
        interval: BillingUsageSeriesInterval | None = None,
        template_id: str | None = None,
        tag_key: str | None = None,
        tag_value: str | None = None,
        request_id: str | None = None,
    ) -> OrganizationUserUsageSummary:
        """One User's usage summary between ``from_`` (inclusive) and ``to`` (exclusive)."""
        path = user_usage_path(user_id, from_, to)
        query = usage_query(
            from_=from_, to=to, interval=interval, template_id=template_id, tag_key=tag_key, tag_value=tag_value
        )
        return await self._http.get(f"{path}{query}", request_id=request_id)

    async def get_user_usage_details(
        self,
        user_id: str,
        *,
        from_: str,
        to: str,
        kind: BillingUsageFactKind | None = None,
        limit: int | None = None,
        cursor: str | None = None,
        template_id: str | None = None,
        tag_key: str | None = None,
        tag_value: str | None = None,
        request_id: str | None = None,
    ) -> OrganizationUserUsageDetails:
        """One User's usage items, globally ordered; ``nextCursor: None`` ends the stream."""
        path = user_usage_path(user_id, from_, to)
        query = usage_query(
            from_=from_,
            to=to,
            kind=kind,
            limit=limit,
            cursor=cursor,
            template_id=template_id,
            tag_key=tag_key,
            tag_value=tag_value,
        )
        return await self._http.get(f"{path}/details{query}", request_id=request_id)

    async def get_organization(self, *, request_id: str | None = None) -> Organization:
        response = await self._http.get("", request_id=request_id)
        return response["organization"]

    async def create_end_user(
        self,
        *,
        display_name: Omittable[str] = OMIT,
        external_id: Omittable[str] = OMIT,
        request_id: str | None = None,
    ) -> EndUser:
        """Create an End User, or return the existing one with the same ``external_id``.

        Calls with an ``external_id`` are retried automatically and are safe to
        repeat after an uncertain outcome. An ``external_id`` that only differs
        by case or accents from an existing one is refused with a ConflictError.
        """
        body: dict[str, str] = {}
        if display_name is not OMIT:
            body["displayName"] = display_name
        if external_id is not OMIT:
            body["externalId"] = external_id
        result = await self._http.post_response(
            "/users",
            body,
            outcome_unknown=True,
            idempotent=external_id is not OMIT,
            request_id=request_id,
        )
        user = result.data.get("user") if isinstance(result.data, dict) else None
        if not isinstance(user, dict) or not isinstance(user.get("userId"), str) or not user["userId"]:
            raise OutcomeUnknownError(
                "End User creation returned no usable User; repeat it with the same external_id",
                request_id=result.request_id,
            )
        return user  # type: ignore[return-value]

    async def list_end_users(self, *, request_id: str | None = None) -> list[EndUser]:
        response = await self._http.get("/users", request_id=request_id)
        return response["users"]

    async def get_end_user(self, user_id: str, *, request_id: str | None = None) -> EndUser:
        response = await self._http.get(f"/users/{segment(user_id)}", request_id=request_id)
        return response["user"]
