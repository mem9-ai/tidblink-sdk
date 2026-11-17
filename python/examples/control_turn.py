"""Cancel a Turn, or retry it from its checkpoint and wait (``control-turn.mjs`` in the TypeScript SDK)."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import sys
from typing import Literal

from tidblink_server import OrganizationClient, SessionInteraction, TurnResult


async def control_turn_workflow(
    *,
    base_url: str,
    organization_api_key: str,
    user_id: str,
    session_id: str,
    turn_id: str,
    client_event_id: str,
    action: Literal["cancel", "retry"],
) -> SessionInteraction | TurnResult:
    """Persist the action and both identities before dispatch; recovery reads that command."""
    if action not in ("cancel", "retry"):
        raise ValueError("action must be cancel or retry")
    async with OrganizationClient(base_url=base_url, api_key=organization_api_key) as organization:
        session = organization.user(user_id).session(session_id)
        if action == "cancel":
            return await session.cancel(client_event_id=client_event_id, turn_id=turn_id)
        accepted = await session.retry(client_event_id=client_event_id, turn_id=turn_id)
        return await session.wait_for_result(accepted["clientEventId"])


def _main() -> int:
    env = os.environ
    try:
        result = asyncio.run(
            control_turn_workflow(
                base_url=env.get("AGENT_SERVICE_URL", ""),
                organization_api_key=env.get("ORGANIZATION_API_KEY", ""),
                user_id=env.get("END_USER_ID", ""),
                session_id=env.get("SESSION_ID", ""),
                turn_id=env.get("TURN_ID", ""),
                client_event_id=env.get("CLIENT_EVENT_ID", ""),
                action=env.get("TURN_ACTION", ""),  # type: ignore[arg-type]
            )
        )
    except Exception as error:
        print(str(error) or "Turn control failed", file=sys.stderr)
        return 1
    print(json.dumps(dataclasses.asdict(result) if dataclasses.is_dataclass(result) else result))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
