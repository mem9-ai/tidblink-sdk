"""Upload a file for an End User and attach it to a message in a Session."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from tidblink_server import OrganizationClient

if TYPE_CHECKING:
    from tidblink_server import TurnResult, UserFile


async def upload_file_workflow(
    *,
    base_url: str,
    organization_api_key: str,
    user_id: str,
    session_id: str,
    original_name: str,
    data: bytes | bytearray | memoryview,
    idempotency_key: str,
    client_event_id: str,
    text: str = "Read the attached file.",
    timeout: float | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> tuple[UserFile, TurnResult]:
    """Persist both identities and exact file/message input before calling."""
    async with OrganizationClient(
        base_url=base_url, api_key=organization_api_key, http_client=http_client
    ) as organization:
        user = organization.user(user_id)
        file = await user.files.upload(
            original_name=original_name, data=data, idempotency_key=idempotency_key
        )
        result = await user.session(session_id).turn(
            client_event_id=client_event_id,
            text=text,
            user_file_ids=[file["fileId"]],
            timeout=timeout,
        )
        return file, result


async def _main() -> int:
    try:
        file_path = os.environ.get("FILE_PATH")
        if not file_path:
            raise ValueError("Set FILE_PATH to the file to attach")
        _, result = await upload_file_workflow(
            base_url=os.environ.get("AGENT_SERVICE_URL"),  # type: ignore[arg-type]
            organization_api_key=os.environ.get("ORGANIZATION_API_KEY"),  # type: ignore[arg-type]
            user_id=os.environ.get("END_USER_ID"),  # type: ignore[arg-type]
            session_id=os.environ.get("SESSION_ID"),  # type: ignore[arg-type]
            original_name=Path(file_path).name,
            data=Path(file_path).read_bytes(),
            idempotency_key=os.environ.get("UPLOAD_IDEMPOTENCY_KEY"),  # type: ignore[arg-type]
            client_event_id=os.environ.get("CLIENT_EVENT_ID"),  # type: ignore[arg-type]
            text=os.environ.get("MESSAGE_TEXT", "Read the attached file."),
        )
        print(result.text)
        return 0
    except Exception as error:
        print(str(error) or "File workflow failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
