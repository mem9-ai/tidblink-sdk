"""Send a text message and wait for its original result (``send-message.mjs`` in the TypeScript SDK)."""

from __future__ import annotations

import asyncio
import os
import sys

from tidblink_server import OrganizationClient, TurnResult


async def send_message_workflow(
    *,
    base_url: str,
    organization_api_key: str,
    user_id: str,
    session_id: str,
    client_event_id: str,
    text: str,
    timeout: float | None = None,
) -> TurnResult:
    """Persist ``session_id``, ``client_event_id`` and ``text`` before calling; reuse them for recovery."""
    async with OrganizationClient(base_url=base_url, api_key=organization_api_key) as organization:
        session = organization.user(user_id).session(session_id)
        return await session.turn(client_event_id=client_event_id, text=text, timeout=timeout)


def _main() -> int:
    env = os.environ
    try:
        result = asyncio.run(
            send_message_workflow(
                base_url=env.get("AGENT_SERVICE_URL", ""),
                organization_api_key=env.get("ORGANIZATION_API_KEY", ""),
                user_id=env.get("END_USER_ID", ""),
                session_id=env.get("SESSION_ID", ""),
                client_event_id=env.get("CLIENT_EVENT_ID", ""),
                text=env.get("MESSAGE_TEXT", ""),
            )
        )
    except Exception as error:
        print(str(error) or "Message failed", file=sys.stderr)
        return 1
    print(result.text)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
