"""Prepare a Session's Realtime Connection for a terminal (``prepare-realtime-connection.mjs`` in the TypeScript SDK)."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Mapping
from typing import Any

from tidblink_server import OMIT, OrganizationClient


async def prepare_realtime_connection(
    *,
    base_url: str,
    organization_api_key: str,
    user_id: str,
    session_id: str,
    realtime_model_id: str | None = None,
    audio: Mapping[str, Any] | None = None,
    metadata: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Prepare an existing owned Session for a terminal to connect; this does not open a WebSocket."""
    async with OrganizationClient(base_url=base_url, api_key=organization_api_key) as organization:
        client = organization.user(user_id)
        catalog = await client.list_realtime_models()
        session = client.session(session_id)
        selection = await session.get_realtime_model_selection()
        if realtime_model_id is not None and realtime_model_id != selection["model"]:
            selection = await session.set_realtime_model(realtime_model_id, if_match=selection["etag"])
        prepared = await session.open_realtime_connection(
            audio=OMIT if audio is None else audio,
            metadata=OMIT if metadata is None else metadata,
        )
        return {"catalog": catalog, "selection": selection, **prepared}


async def _main() -> None:
    env = os.environ
    if not all(env.get(name) for name in ("AGENT_SERVICE_URL", "ORGANIZATION_API_KEY", "END_USER_ID", "SESSION_ID")):
        raise SystemExit(
            "Set AGENT_SERVICE_URL, ORGANIZATION_API_KEY, END_USER_ID and SESSION_ID; REALTIME_MODEL_ID is optional"
        )
    result = await prepare_realtime_connection(
        base_url=env["AGENT_SERVICE_URL"],
        organization_api_key=env["ORGANIZATION_API_KEY"],
        user_id=env["END_USER_ID"],
        session_id=env["SESSION_ID"],
        realtime_model_id=env.get("REALTIME_MODEL_ID") or None,
    )
    # The one-time connect token is returned to imported callers but never printed by this CLI.
    print(
        json.dumps(
            {
                "realtimeConnection": result["realtimeConnection"],
                "websocketUrl": result["websocketUrl"],
                "connectTokenExpiresAt": result["connectTokenExpiresAt"],
            }
        )
    )


if __name__ == "__main__":
    asyncio.run(_main())
