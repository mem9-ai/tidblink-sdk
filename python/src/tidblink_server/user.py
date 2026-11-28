"""Resources of one selected End User (``user.ts`` in the TypeScript SDK)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, TypedDict

from ._http import HttpClient, pagination_query, query_string, template_filter
from ._types import OMIT, Omittable
from .client_tools import ClientToolDeclaration
from .files import UserFiles
from .history import BillingTag
from .realtime import RealtimeModelCatalog
from .session import Session, SessionSandboxSelection, create_session


class AgentTemplate(TypedDict):
    agentTemplateId: str
    displayName: str
    description: str
    contentDigest: str


class AgentTemplatePage(TypedDict):
    templates: list[AgentTemplate]
    hasMore: bool
    nextCursor: str | None


class AgentModel(TypedDict):
    model: str
    label: str
    availability: Literal["available", "provider_unconfigured"]


class AgentModelCatalog(TypedDict):
    defaultModel: str
    models: list[AgentModel]
    organizationDefaultModel: str
    effectiveDefaultModel: str
    organizationAllowedModels: list[str]
    effectiveAllowedModels: list[str]


@dataclass(frozen=True)
class SessionPage:
    """A page of Sessions; ``sessions`` holds :class:`Session` handles with their records."""

    sessions: list[Session]
    has_more: bool
    next_cursor: str | None


class EndUserClient:
    """Ordinary End User resources under the Organization API Key's authority.

    Selecting a User sends no request.
    """

    def __init__(self, http: HttpClient) -> None:
        self._http = http
        self._files = UserFiles(http)

    @property
    def user_id(self) -> str:
        assert self._http.user_id is not None
        return self._http.user_id

    @property
    def files(self) -> UserFiles:
        return self._files

    async def create_session(
        self,
        *,
        template_id: str,
        idempotency_key: str,
        model: Omittable[str] = OMIT,
        realtime_model: Omittable[str] = OMIT,
        realtime_voice: Omittable[str] = OMIT,
        billing_tag: Omittable[BillingTag | None] = OMIT,
        tools: Omittable[Sequence[ClientToolDeclaration]] = OMIT,
        additional_instructions: Omittable[str | None] = OMIT,
        sandbox: Omittable[SessionSandboxSelection] = OMIT,
        acknowledge_session_recall: Omittable[bool] = OMIT,
        request_id: str | None = None,
    ) -> Session:
        """Create a Session from an explicit Template.

        The Session freezes the Template recipe, digest and these creation
        options. The same ``idempotency_key`` and input recover the original
        Session; every option takes part in that identity.
        """
        return await create_session(
            self._http,
            template_id=template_id,
            idempotency_key=idempotency_key,
            model=model,
            realtime_model=realtime_model,
            realtime_voice=realtime_voice,
            billing_tag=billing_tag,
            tools=tools,
            additional_instructions=additional_instructions,
            sandbox=sandbox,
            acknowledge_session_recall=acknowledge_session_recall,
            request_id=request_id,
        )

    async def list_agent_templates(
        self, *, limit: int | None = None, cursor: str | None = None, request_id: str | None = None
    ) -> AgentTemplatePage:
        return await self._http.get(f"/agent-templates{pagination_query(limit, cursor)}", request_id=request_id)

    async def list_models(self, *, request_id: str | None = None) -> AgentModelCatalog:
        return await self._http.get("/agent-models", request_id=request_id)

    async def list_realtime_models(self, *, request_id: str | None = None) -> RealtimeModelCatalog:
        return await self._http.get("/realtime-models", request_id=request_id)

    async def list_sessions(
        self,
        *,
        limit: int | None = None,
        cursor: str | None = None,
        template_id: str | None = None,
        request_id: str | None = None,
    ) -> SessionPage:
        """List Sessions; ``template_id`` keeps only Sessions currently on that Template."""
        pagination_query(limit, cursor)
        query = query_string({"limit": limit, "cursor": cursor, **template_filter(template_id)})
        response = await self._http.get(f"/sessions{query}", request_id=request_id)
        return SessionPage(
            sessions=[Session(self._http, record["sessionId"], record) for record in response["sessions"]],
            has_more=response["hasMore"],
            next_cursor=response["nextCursor"],
        )

    def session(self, id: str) -> Session:
        """A handle to a Session, without a request."""
        return Session(self._http, id)

    async def get_session(self, id: str, *, request_id: str | None = None) -> Session:
        return await self.session(id).refresh(request_id=request_id)
