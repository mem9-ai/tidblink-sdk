"""Organization Agent Templates (``templates.ts`` in the TypeScript SDK).

The SDK returns the service's values unchanged.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, NotRequired, TypedDict

from ._http import HttpClient, HttpResult, query_string, require_creation_key, segment
from .errors import OutcomeUnknownError

__all__ = [
    "Templates",
    "Template",
    "TemplateSummary",
    "TemplateContent",
    "TemplateRealtimeLanguage",
    "TemplatePage",
    "TemplateCatalog",
]

TemplateRealtimeLanguage = Literal["zh-CN", "en-US"]


class TemplateIdentity(TypedDict):
    agentName: str
    description: NotRequired[str]


class TemplateSandbox(TypedDict):
    profileId: str


class TemplateRealtime(TypedDict):
    language: TemplateRealtimeLanguage


class TemplateSkillReference(TypedDict):
    skillId: str
    version: int


class TemplateMcpServerReference(TypedDict):
    #: An MCP Server Registration's ID, or a Connector's ``connectorId``.
    serverId: str
    #: For an MCP Server Registration only: limits the tools it exposes; omit it to expose every tool.
    toolNames: NotRequired[list[str]]


class TemplateTools(TypedDict):
    mcp: list[TemplateMcpServerReference]


class TemplateStandingTask(TypedDict):
    goal: str


class TemplateContent(TypedDict):
    """The definition of an Agent Template.

    Fields the SDK does not model are returned and accepted unchanged.
    """

    schemaVersion: NotRequired[Literal["agent-template-content@7"]]
    identity: TemplateIdentity
    instructions: NotRequired[str]
    #: A Sandbox profile from ``templates.catalog()``.
    sandbox: TemplateSandbox
    #: The language of voice conversations on Sessions created from it; None turns voice off.
    realtime: TemplateRealtime | None
    #: Published Skill versions from ``skills`` or ``templates.catalog()``.
    skills: NotRequired[list[TemplateSkillReference]]
    capabilities: NotRequired[dict[str, Any]]
    #: Organization MCP Servers and Connectors the Template uses, by ``serverId``
    #: (a Connector's ``connectorId``). On replace, omitting ``tools`` keeps the
    #: current references.
    tools: NotRequired[TemplateTools]
    #: A task the voice model keeps working on across a Realtime connection;
    #: ignored when ``realtime`` is None.
    standingTask: NotRequired[TemplateStandingTask]


class TemplateSummary(TypedDict):
    id: str
    organizationId: str
    name: str
    displayName: str
    sandboxProfile: str
    lifecycleStatus: Literal["active", "archived"]
    #: Pass this exact value as ``if_match`` to replace or archive the Template.
    etag: str
    contentDigest: str | None
    createdAt: str
    updatedAt: str


class Template(TypedDict):
    template: TemplateSummary
    content: TemplateContent


class TemplatePage(TypedDict):
    data: list[TemplateSummary]
    hasMore: bool
    nextCursor: str | None
    readAt: str


class TemplateCatalogModel(TypedDict):
    id: str
    available: bool


class TemplateCatalogSandbox(TypedDict):
    id: str
    label: str
    capabilities: list[str]


class TemplateCatalogSkill(TypedDict):
    skillId: str
    name: str
    description: str
    version: int
    available: bool


class TemplateCatalog(TypedDict):
    """What a Template may reference in this Organization."""

    models: list[TemplateCatalogModel]
    sandboxes: list[TemplateCatalogSandbox]
    skills: list[TemplateCatalogSkill]


TemplateSort = Literal["updated_desc", "created_desc", "name_asc"]
TemplateLifecycleFilter = Literal["all", "active", "archived"]


def _template_path(template_id: str) -> str:
    return f"/agent-templates/{segment(template_id)}"


def _template_receipt(result: HttpResult) -> Template:
    data = result.data
    template = data.get("template") if isinstance(data, dict) else None
    if (
        not isinstance(template, dict)
        or not isinstance(template.get("id"), str)
        or not template["id"]
        or not isinstance(data.get("content"), dict)
    ):
        raise OutcomeUnknownError(
            "Template mutation returned no usable Template; repeat it with the same idempotency key",
            request_id=result.request_id,
        )
    return data  # type: ignore[no-any-return]


def _template_body(display_name: object, content: object, label: str) -> dict[str, Any]:
    if not isinstance(display_name, str):
        raise TypeError(f"{label} requires a display_name")
    if not display_name.strip():
        raise ValueError(f"{label} requires a display_name")
    if not isinstance(content, Mapping):
        raise TypeError(f"{label} requires content")
    return {"displayName": display_name, "content": dict(content)}


def _require_if_match(if_match: object, label: str) -> str:
    if not isinstance(if_match, str):
        raise TypeError(f"{label} requires if_match")
    if not if_match:
        raise ValueError(f"{label} requires if_match")
    return if_match


def _optional_text(value: object, name: str) -> None:
    if value is not None and not isinstance(value, str):
        raise TypeError(f"{name} must be text")


def _optional_integer(value: object, name: str) -> None:
    if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
        raise TypeError(f"{name} must be an integer")


class Templates:
    """Organization Agent Templates: what Sessions are created from."""

    def __init__(self, http: HttpClient) -> None:
        self._http = http

    async def list(
        self,
        *,
        sort: TemplateSort | None = None,
        lifecycle_status: TemplateLifecycleFilter | None = None,
        q: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
        request_id: str | None = None,
    ) -> TemplatePage:
        """A page of Templates. ``lifecycle_status`` defaults to the service's choice; ``q`` searches names."""
        for name, value in (("sort", sort), ("lifecycle_status", lifecycle_status), ("q", q), ("cursor", cursor)):
            _optional_text(value, name)
        _optional_integer(limit, "limit")
        query = query_string(
            {"sort": sort, "lifecycleStatus": lifecycle_status, "q": q, "limit": limit, "cursor": cursor}
        )
        return await self._http.get(f"/agent-templates{query}", request_id=request_id)

    async def get(self, template_id: str, *, request_id: str | None = None) -> Template:
        return await self._http.get(_template_path(template_id), request_id=request_id)

    async def catalog(self, *, limit: int | None = None, request_id: str | None = None) -> TemplateCatalog:
        """Models, Sandbox profiles and published Skills a Template may use."""
        _optional_integer(limit, "limit")
        query = query_string({"limit": limit})
        return await self._http.get(f"/agent-template-reference-catalog{query}", request_id=request_id)

    async def create(
        self,
        *,
        display_name: str,
        content: TemplateContent | Mapping[str, Any],
        idempotency_key: str,
        request_id: str | None = None,
    ) -> Template:
        """Create a Template. Keep the same key and input when recovering an uncertain creation."""
        body = _template_body(display_name, content, "Template creation")
        require_creation_key(idempotency_key)
        return _template_receipt(
            await self._http.post_response(
                "/agent-templates",
                body,
                idempotency_key=idempotency_key,
                idempotent=True,
                outcome_unknown=True,
                request_id=request_id,
            )
        )

    async def replace(
        self,
        template_id: str,
        *,
        display_name: str,
        content: TemplateContent | Mapping[str, Any],
        if_match: str,
        idempotency_key: str,
        request_id: str | None = None,
    ) -> Template:
        """Replace the whole definition. Sessions already created keep their snapshot.

        ``if_match`` is the ``etag`` you read; a stale value is refused with
        ConflictError. Keep the same key and input when recovering an
        uncertain change.
        """
        body = _template_body(display_name, content, "Template replacement")
        _require_if_match(if_match, "Template replacement")
        require_creation_key(idempotency_key)
        return _template_receipt(
            await self._http.response(
                _template_path(template_id),
                method="PUT",
                body=body,
                if_match=if_match,
                idempotency_key=idempotency_key,
                retry="safe",
                outcome_unknown=True,
                request_id=request_id,
            )
        )

    async def archive(
        self,
        template_id: str,
        *,
        if_match: str,
        idempotency_key: str,
        request_id: str | None = None,
    ) -> Template:
        """Archive the Template; it can no longer be used to create Sessions."""
        _require_if_match(if_match, "Template archive")
        require_creation_key(idempotency_key)
        return _template_receipt(
            await self._http.post_response(
                f"{_template_path(template_id)}/archive",
                {},
                if_match=if_match,
                idempotency_key=idempotency_key,
                idempotent=True,
                outcome_unknown=True,
                request_id=request_id,
            )
        )
