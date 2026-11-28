"""An End User's Agents (``gated/agents.ts`` in the TypeScript SDK).

An Agent is a named preset of Session creation settings that creates ordinary
Sessions. Only Organizations enabled for Agents may use them; others receive
:class:`~tidblink_server.gated.FeatureNotEnabledError` from every call.
"""

from __future__ import annotations

from collections.abc import Awaitable, Sequence
from typing import Any, Required, TypedDict, TypeVar

from .._http import (
    HttpClient,
    HttpResult,
    pagination_query,
    require_creation_key,
    require_etag,
    require_known_keys,
    require_template_id,
    segment,
)
from .._types import OMIT, Omittable
from ..client_tools import ClientToolDeclaration, validate_client_tools
from ..errors import OutcomeUnknownError, TiDBLinkApiError
from ..history import BillingTag
from ..session import Session, SessionSandboxSelection
from ..user import EndUserClient, SessionPage
from .errors import raise_gated

T = TypeVar("T")


class AgentSessionDefaults(TypedDict, total=False):
    """The Session creation settings an Agent stores.

    The keys are :meth:`EndUserClient.create_session`'s keyword arguments
    without ``idempotency_key`` and ``tools``, so
    ``user.create_session(**agent["sessionDefaults"], idempotency_key=key)``
    creates the same Session without the Agent.
    """

    template_id: Required[str]
    model: str
    realtime_model: str
    realtime_voice: str
    billing_tag: BillingTag | None
    additional_instructions: str | None
    sandbox: SessionSandboxSelection
    acknowledge_session_recall: bool


class AgentRecord(TypedDict):
    """An Agent as listed."""

    agentId: str
    #: The End User who owns the Agent.
    userId: str
    name: str
    #: Stored as accepted; Session creation checks them again.
    sessionDefaults: AgentSessionDefaults
    createdAt: str
    updatedAt: str


class Agent(AgentRecord):
    """An Agent read by itself, with the revision :meth:`Agents.update` needs."""

    #: Pass this exact value as ``if_match`` to update the Agent.
    etag: str


class AgentPage(TypedDict):
    """Newest first. Listed Agents carry no ``etag``; :meth:`Agents.get` one to update it."""

    agents: list[AgentRecord]
    hasMore: bool
    nextCursor: str | None


# Session creation's wire names, by the keyword argument that sends each.
_WIRE_NAMES = {
    "template_id": "agentTemplateId",
    "model": "model",
    "realtime_model": "realtimeModel",
    "realtime_voice": "realtimeVoice",
    "billing_tag": "billingTag",
    "additional_instructions": "additionalInstructions",
    "sandbox": "sandbox",
    "acknowledge_session_recall": "acknowledgeSessionRecall",
}
_SDK_NAMES = {wire: name for name, wire in _WIRE_NAMES.items()}


def _to_wire(defaults: object, label: str) -> dict[str, Any]:
    checked = require_known_keys(defaults, tuple(_WIRE_NAMES), f"{label} session_defaults")
    if "template_id" not in checked:
        raise TypeError(f"{label} session_defaults requires template_id")
    require_template_id(checked["template_id"], label)
    return {_WIRE_NAMES[key]: value for key, value in checked.items()}


def _from_wire(agent: dict[str, Any]) -> dict[str, Any]:
    defaults = {_SDK_NAMES.get(key, key): value for key, value in agent["sessionDefaults"].items()}
    return {**agent, "sessionDefaults": defaults}


def _agent_path(agent_id: object) -> str:
    if not isinstance(agent_id, str):
        raise TypeError("An Agent ID is required")
    if not agent_id:
        raise ValueError("An Agent ID is required")
    return f"/agents/{segment(agent_id)}"


def _require_name(name: object, label: str) -> str:
    if not isinstance(name, str):
        raise TypeError(f"{label} requires a name")
    if not name.strip():
        raise ValueError(f"{label} requires a name")
    return name


def _agent_receipt(result: HttpResult, missing: str) -> Agent:
    agent = result.data.get("agent") if isinstance(result.data, dict) else None
    if not isinstance(agent, dict) or not isinstance(agent.get("agentId"), str) or not agent["agentId"] or not result.etag:
        raise OutcomeUnknownError(missing, request_id=result.request_id)
    return {**_from_wire(agent), "etag": result.etag}  # type: ignore[return-value]


async def _gated(call: Awaitable[T]) -> T:
    try:
        return await call
    except TiDBLinkApiError as error:
        raise_gated(error)


class Agents:
    """One End User's Agents. Get them with :func:`agents`."""

    def __init__(self, http: HttpClient) -> None:
        self._http = http

    async def create(
        self,
        *,
        name: str,
        session_defaults: AgentSessionDefaults,
        idempotency_key: str,
        request_id: str | None = None,
    ) -> Agent:
        """Create an Agent; its Template must currently be selectable.

        Repeating the key and input returns the Agent that key created.
        """
        _require_name(name, "Agent creation")
        require_creation_key(idempotency_key, "Agent creation")
        body = {"name": name, "sessionDefaults": _to_wire(session_defaults, "Agent creation")}
        result = await _gated(
            self._http.post_response(
                "/agents",
                body,
                idempotency_key=idempotency_key,
                idempotent=True,
                outcome_unknown=True,
                request_id=request_id,
            )
        )
        return _agent_receipt(result, "Agent creation returned no usable Agent; recover with the same creation key")

    async def list(
        self, *, limit: int | None = None, cursor: str | None = None, request_id: str | None = None
    ) -> AgentPage:
        """The End User's Agents, newest first."""
        page = await _gated(self._http.get(f"/agents{pagination_query(limit, cursor)}", request_id=request_id))
        return {**page, "agents": [_from_wire(agent) for agent in page["agents"]]}  # type: ignore[return-value]

    async def get(self, agent_id: str, *, request_id: str | None = None) -> Agent:
        result = await _gated(self._http.get_response(_agent_path(agent_id), request_id=request_id))
        return {**_from_wire(result.data["agent"]), "etag": require_etag(result)}  # type: ignore[return-value]

    async def update(
        self,
        agent_id: str,
        *,
        if_match: str,
        name: Omittable[str] = OMIT,
        session_defaults: Omittable[AgentSessionDefaults] = OMIT,
        request_id: str | None = None,
    ) -> Agent:
        """Change the name or the Session defaults; ``session_defaults`` replaces them whole.

        Sessions created afterwards use the change; existing Sessions keep
        their configuration. A stale ``if_match`` raises :class:`ConflictError`.
        """
        path = _agent_path(agent_id)
        if not isinstance(if_match, str):
            raise TypeError("Agent update requires if_match")
        if not if_match:
            raise ValueError("Agent update requires if_match")
        body: dict[str, Any] = {}
        if name is not OMIT:
            body["name"] = _require_name(name, "Agent update")
        if session_defaults is not OMIT:
            body["sessionDefaults"] = _to_wire(session_defaults, "Agent update")
        result = await _gated(
            self._http.patch_response(
                path, body, if_match=if_match, outcome_unknown=True, request_id=request_id
            )
        )
        return _agent_receipt(
            result, "Agent update returned no usable Agent; read the Agent before changing it again"
        )

    async def delete(self, agent_id: str, *, request_id: str | None = None) -> None:
        """Delete the Agent. Sessions created through it remain ordinary Sessions."""
        await _gated(
            self._http.delete_response(_agent_path(agent_id), outcome_unknown=True, request_id=request_id)
        )

    async def create_session(
        self,
        agent_id: str,
        *,
        idempotency_key: str,
        model: Omittable[str] = OMIT,
        realtime_model: Omittable[str] = OMIT,
        realtime_voice: Omittable[str | None] = OMIT,
        billing_tag: Omittable[BillingTag | None] = OMIT,
        tools: Omittable[Sequence[ClientToolDeclaration]] = OMIT,
        request_id: str | None = None,
    ) -> Session:
        """Create an ordinary Session from the Agent's defaults and these choices.

        The key shares the namespace of :meth:`EndUserClient.create_session`:
        a retry returns the original Session even after the Agent changed.
        """
        path = f"{_agent_path(agent_id)}/sessions"
        require_creation_key(idempotency_key, "Session creation")
        body: dict[str, Any] = {}
        if model is not OMIT:
            body["model"] = model
        if realtime_model is not OMIT:
            body["realtimeModel"] = realtime_model
        if realtime_voice is not OMIT:
            # None drops the Agent's voice, so the Session speaks the default voice.
            body["realtimeVoice"] = realtime_voice
        if billing_tag is not OMIT:
            body["billingTag"] = billing_tag
        if tools is not OMIT:
            body["tools"] = validate_client_tools(tools)
        result = await _gated(
            self._http.post_response(
                path,
                body,
                idempotency_key=idempotency_key,
                idempotent=True,
                outcome_unknown=True,
                request_id=request_id,
            )
        )
        record = result.data.get("session") if isinstance(result.data, dict) else None
        if not isinstance(record, dict) or not isinstance(record.get("sessionId"), str) or not record["sessionId"]:
            raise OutcomeUnknownError(
                "Session creation returned no usable Session; recover with the same creation key",
                request_id=result.request_id,
            )
        if not result.etag:
            raise OutcomeUnknownError(
                "Session creation returned no model ETag; recover with the same creation key",
                request_id=result.request_id,
            )
        return Session(self._http, record["sessionId"], record, result.etag)  # type: ignore[arg-type]

    async def list_sessions(
        self,
        agent_id: str,
        *,
        limit: int | None = None,
        cursor: str | None = None,
        request_id: str | None = None,
    ) -> SessionPage:
        """The Sessions created through the Agent that still exist, newest first."""
        path = f"{_agent_path(agent_id)}/sessions{pagination_query(limit, cursor)}"
        page = await _gated(self._http.get(path, request_id=request_id))
        return SessionPage(
            sessions=[Session(self._http, record["sessionId"], record) for record in page["sessions"]],
            has_more=page["hasMore"],
            next_cursor=page["nextCursor"],
        )


def agents(user: EndUserClient) -> Agents:
    """The Agents of ``user``, from ``organization.user(user_id)``.

    Sends no request: an Organization that is not enabled for Agents learns so
    from the first call, as :class:`~tidblink_server.gated.FeatureNotEnabledError`.
    """
    if not isinstance(user, EndUserClient):
        raise TypeError("Expected an EndUserClient from organization.user(user_id)")
    return Agents(user._http)
