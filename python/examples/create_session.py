"""Create a Session from a selected Template, recoverably (``create-session.mjs`` in the TypeScript SDK)."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from tidblink_server import OrganizationClient, SessionSandboxSelection


async def create_session_workflow(
    *,
    base_url: str,
    organization_api_key: str,
    user_id: str,
    template_id: str,
    session_creation_key: str,
    model: str | None = None,
    additional_instructions: str | None = None,
    sandbox: SessionSandboxSelection | None = None,
    acknowledge_session_recall: bool | None = None,
) -> dict[str, Any]:
    """Retain the owner, selected Template, optional Session options and the creation key before calling.

    Pass ``acknowledge_session_recall=True`` when the Template enables
    cross-Session Recall. The returned ``organization`` stays open: close it
    when done.
    """
    if not template_id:
        raise ValueError("Select and retain a template_id before creating a Session")
    organization = OrganizationClient(base_url=base_url, api_key=organization_api_key)
    try:
        user = organization.user(user_id)
        templates = await user.list_agent_templates()
        models = await user.list_models()
        # Omission is part of the creation identity; resolving today's default would change a replay.
        optional: dict[str, Any] = {
            "model": model,
            "additional_instructions": additional_instructions,
            "sandbox": sandbox,
            "acknowledge_session_recall": acknowledge_session_recall,
        }
        session = await user.create_session(
            template_id=template_id,
            idempotency_key=session_creation_key,
            **{name: value for name, value in optional.items() if value is not None},
        )
    except BaseException:
        await organization.close()
        raise
    return {
        "organization": organization,
        "user": user,
        "session": session,
        "templates": templates,
        "models": models,
    }


async def _main() -> None:
    env = os.environ
    required = ("AGENT_SERVICE_URL", "ORGANIZATION_API_KEY", "END_USER_ID", "TEMPLATE_ID", "SESSION_CREATION_KEY")
    if not all(env.get(name) for name in required):
        raise SystemExit(
            "Set AGENT_SERVICE_URL, ORGANIZATION_API_KEY, END_USER_ID, TEMPLATE_ID and SESSION_CREATION_KEY; "
            "MODEL_ID and ACKNOWLEDGE_SESSION_RECALL=true are optional"
        )
    result = await create_session_workflow(
        base_url=env["AGENT_SERVICE_URL"],
        organization_api_key=env["ORGANIZATION_API_KEY"],
        user_id=env["END_USER_ID"],
        session_creation_key=env["SESSION_CREATION_KEY"],
        template_id=env["TEMPLATE_ID"],
        model=env.get("MODEL_ID") or None,
        acknowledge_session_recall=True if env.get("ACKNOWLEDGE_SESSION_RECALL") == "true" else None,
    )
    async with result["organization"]:
        print(json.dumps({"sessionId": result["session"].id, "model": result["session"].model}))


if __name__ == "__main__":
    asyncio.run(_main())
