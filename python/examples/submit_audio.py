"""Submit a prerecorded recording as an audio Turn and print its final answer."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from tidblink_server import OMIT, BillingTag, Omit, OrganizationClient
from tidblink_server import AudioTurnResult


async def submit_audio_workflow(
    *,
    base_url: str,
    organization_api_key: str,
    user_id: str,
    session_id: str,
    data: bytes,
    filename: str,
    content_type: str | None = None,
    billing_tag: BillingTag | None | Omit = OMIT,
) -> AudioTurnResult:
    """This recording request owns its response; cancelling it may interrupt the Turn."""
    async with OrganizationClient(base_url=base_url, api_key=organization_api_key) as organization:
        return await organization.user(user_id).session(session_id).audio(
            data,
            filename=filename,
            content_type=content_type,
            billing_tag=billing_tag,
        )


async def main() -> int:
    try:
        path = os.environ.get("AUDIO_FILE_PATH")
        if not path:
            raise ValueError("Set AUDIO_FILE_PATH to an existing recording")
        result = await submit_audio_workflow(
            base_url=os.environ.get("AGENT_SERVICE_URL", ""),
            organization_api_key=os.environ.get("ORGANIZATION_API_KEY", ""),
            user_id=os.environ.get("END_USER_ID", ""),
            session_id=os.environ.get("SESSION_ID", ""),
            filename=Path(path).name,
            data=Path(path).read_bytes(),
            content_type=os.environ.get("AUDIO_CONTENT_TYPE"),
        )
        print(result.text)
        return 0
    except Exception as error:
        print(str(error) or "Audio request failed", file=sys.stderr)
        turn_id = getattr(error, "turn_id", None)
        if isinstance(turn_id, str):
            print(f"Turn: {turn_id}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
