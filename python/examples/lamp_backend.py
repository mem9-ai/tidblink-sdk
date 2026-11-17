"""The backend of a smart lamp that users talk to by typing in an app or by speaking to the lamp.

Both share one Session, so one conversation history. See ``examples/lamp/README.md``.

- The lamp's functions are Client Tools: the model calls them, this backend
  runs them by forwarding to the lamp over your own device channel (MQTT,
  WebSocket, ...), and the service never talks to the lamp except for audio.
- While the lamp is online the backend keeps its Session connected, so the
  lamp's tools work for voice too. Typing while the lamp is offline still
  works; its tools then fail at once and the model says so.
- For voice the backend hands the lamp a short-lived grant; the lamp opens
  the Realtime WebSocket itself and audio never passes through here.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import httpx

from tidblink_server import OrganizationClient
from tidblink_server.realtime import RealtimeConnectGrant
from tidblink_server.session import Session
from tidblink_server import ClientToolCallContext, ClientToolHandler, SessionConnection


class Device(Protocol):
    """Your channel to the lamps."""

    def is_online(self, device_id: str) -> bool: ...

    async def request(self, device_id: str, command: str, args: dict[str, Any]) -> Any:
        """Send a command and return the lamp's result; raise when the lamp reports a failure.

        It must stop when its task is cancelled, as happens at a tool call's deadline.
        """
        ...

    def send(self, device_id: str, topic: str, payload: Any) -> None:
        """Fire and forget, e.g. the voice grant."""
        ...


@dataclass(frozen=True)
class LampBinding:
    user_id: str
    session_id: str


class LampStore(Protocol):
    """Who owns which lamp, and where each Session's events were read."""

    async def bind(self, device_id: str, binding: LampBinding) -> None: ...

    async def binding(self, device_id: str) -> LampBinding | None: ...

    async def cursor(self, device_id: str) -> str | None: ...

    async def save_cursor(self, device_id: str, cursor: str) -> None: ...


class MemoryStore:
    """In-memory bindings and cursors; use your database in production."""

    def __init__(self) -> None:
        self._bindings: dict[str, LampBinding] = {}
        self._cursors: dict[str, str] = {}

    async def bind(self, device_id: str, binding: LampBinding) -> None:
        self._bindings[device_id] = binding

    async def binding(self, device_id: str) -> LampBinding | None:
        return self._bindings.get(device_id)

    async def cursor(self, device_id: str) -> str | None:
        return self._cursors.get(device_id)

    async def save_cursor(self, device_id: str, cursor: str) -> None:
        self._cursors[device_id] = cursor


@dataclass(frozen=True)
class ChatLine:
    role: Literal["user", "assistant"]
    text: str


OnChat = Callable[[str, ChatLine], Awaitable[None] | None]


class LampBackend:
    def __init__(
        self,
        *,
        base_url: str,
        organization_api_key: str,
        template_id: str,
        device: Device,
        store: LampStore | None = None,
        on_chat: OnChat | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        """``http_client`` is passed to the OrganizationClient, e.g. for a proxy or tests."""
        self.organization = OrganizationClient(base_url=base_url, api_key=organization_api_key, http_client=http_client)
        self._template_id = template_id
        self._device = device
        self._store: LampStore = store if store is not None else MemoryStore()
        self._on_chat = on_chat
        #: device_id -> SessionConnection while the lamp is online.
        self._online: dict[str, SessionConnection] = {}

    # The lamp's functions (edge capabilities) -----------------------------------

    def _lamp_tools(self, device_id: str) -> dict[str, ClientToolHandler]:
        device = self._device

        async def ask(command: str, args: dict[str, Any]) -> Any:
            # Fail at once instead of letting the model wait for a lamp that is not there.
            if not device.is_online(device_id):
                raise RuntimeError("The lamp is offline")
            return await device.request(device_id, command, args)

        async def set_light(args: dict[str, Any], context: ClientToolCallContext) -> str:
            state = await ask("set_light", args)
            if state["on"]:
                return f"The lamp is on at {state['brightness']}% and {state['colorTemp']} K"
            return "The lamp is off"

        async def get_status(args: dict[str, Any], context: ClientToolCallContext) -> str:
            return _json(await ask("get_status", {}))

        async def set_timer(args: dict[str, Any], context: ClientToolCallContext) -> str:
            minutes = args.get("minutes")
            await ask("set_timer", {"minutes": minutes, "action": "off"})
            return f"The lamp turns off in {minutes} minutes"

        return {
            "set_light": ClientToolHandler(
                description="Turn the lamp on or off, or set its brightness and colour temperature",
                parameters={
                    "type": "object",
                    "properties": {
                        "on": {"type": "boolean", "description": "true turns the lamp on, false off"},
                        "brightness": {"type": "integer", "minimum": 1, "maximum": 100, "description": "Percent"},
                        "colorTemp": {"type": "integer", "minimum": 2700, "maximum": 6500, "description": "Kelvin"},
                    },
                    "additionalProperties": False,
                },
                run=set_light,
            ),
            "get_status": ClientToolHandler(
                description="Read the lamp's state and the ambient light",
                parameters={"type": "object", "properties": {}, "additionalProperties": False},
                run=get_status,
            ),
            "set_timer": ClientToolHandler(
                description="Turn the lamp off after some minutes",
                parameters={
                    "type": "object",
                    "properties": {"minutes": {"type": "integer", "minimum": 1, "maximum": 240}},
                    "required": ["minutes"],
                    "additionalProperties": False,
                },
                run=set_timer,
            ),
        }

    async def _session_of(self, device_id: str) -> Session:
        binding = await self._store.binding(device_id)
        if binding is None:
            raise LookupError(f"Lamp {device_id} is not bound to a user")
        return self.organization.user(binding.user_id).session(binding.session_id)

    async def _chat(self, device_id: str, line: ChatLine) -> None:
        if self._on_chat is not None:
            result = self._on_chat(device_id, line)
            if inspect.isawaitable(result):
                await result

    async def _connect(self, device_id: str) -> SessionConnection:
        session = await self._session_of(device_id)

        async def on_cursor(cursor: str) -> None:
            await self._store.save_cursor(device_id, cursor)

        return await session.connect(
            tools=self._lamp_tools(device_id),
            cursor=await self._store.cursor(device_id),
            on_cursor=on_cursor,
            # What was said by voice also appears in the app.
            on_transcript=lambda t: self._chat(device_id, ChatLine("user", t["text"])),
            on_response=lambda r: self._chat(device_id, ChatLine("assistant", r["text"])),
        )

    # Operations ---------------------------------------------------------------------

    async def bind_device(self, app_user_id: str, device_id: str) -> str:
        """The user binds a lamp in the app: one End User per app user, one Session per lamp."""
        user = await self.organization.create_end_user(external_id=app_user_id)  # safe to repeat
        session = await self.organization.user(user["userId"]).create_session(
            template_id=self._template_id,
            idempotency_key=f"lamp.{device_id}",  # one Session per lamp, also after a retry
        )
        await self._store.bind(device_id, LampBinding(user_id=user["userId"], session_id=session.id))
        return session.id

    async def on_device_online(self, device_id: str) -> None:
        """The lamp came online: keep its Session connected while it stays online."""
        if device_id in self._online:
            return
        conn = await self._connect(device_id)
        self._online[device_id] = conn

        # Another backend process took the lamp over, or the Session was deleted.
        def forget(_: object) -> None:
            if self._online.get(device_id) is conn:
                del self._online[device_id]

        conn.closed.add_done_callback(forget)

    async def on_device_offline(self, device_id: str) -> None:
        """The lamp went offline: release its Session."""
        conn = self._online.pop(device_id, None)
        if conn is not None:
            await conn.close()

    async def on_app_message(
        self,
        device_id: str,
        text: str,
        *,
        client_event_id: str | None = None,
        on_draft: Callable[[str], Awaitable[None] | None] | None = None,
    ) -> str:
        """The user typed in the app.

        Uses the lamp's connection when it is online, otherwise a temporary
        one. ``on_draft`` receives the growing answer.
        """
        existing = self._online.get(device_id)
        conn = existing if existing is not None else await self._connect(device_id)
        try:
            reply = await conn.send(text, client_event_id=client_event_id, on_progress=on_draft)
            return reply.text
        finally:
            if existing is None:
                await conn.close()

    async def on_device_wants_to_talk(self, device_id: str) -> RealtimeConnectGrant:
        """The user pressed the talk button: give the lamp a short-lived grant to open voice."""
        conn = self._online.get(device_id)
        if conn is None:
            raise LookupError(f"Lamp {device_id} is not online")
        grant = await conn.realtime_grant()
        self._device.send(device_id, "voice_grant", grant)
        return grant

    async def close(self) -> None:
        """Release every Session and close the Backend Connections, e.g. on shutdown."""
        self._online.clear()
        await self.organization.close()


def create_lamp_backend(
    *,
    base_url: str,
    organization_api_key: str,
    template_id: str,
    device: Device,
    store: LampStore | None = None,
    on_chat: OnChat | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> LampBackend:
    return LampBackend(
        base_url=base_url,
        organization_api_key=organization_api_key,
        template_id=template_id,
        device=device,
        store=store,
        on_chat=on_chat,
        http_client=http_client,
    )


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"))
