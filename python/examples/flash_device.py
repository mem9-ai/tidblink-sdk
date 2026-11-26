"""Let the Agent flash firmware it built onto a device within the same Turn (``flash-device.mjs`` in the TypeScript SDK).

The Agent writes the image in its Session's Drive, announces it with
``notify_file``, then calls the ``flash_device`` Client Tool with the
announced ``path`` and ``version``. Tell the model so in the Template's
instructions.

- ``on_file`` receives each File Notification, for example to show the user
  what was built. It asks for no reply.
- ``flash_device`` mints a download ticket pinned to the announced version
  and hands its URL to the device, which downloads the image without an API
  Key within 60 seconds. If the Agent changed the file after announcing it,
  minting raises ``DriveFileVersionConflictError``, whose message the model
  sees.
- The device's answer is the tool's result, so the model can react in the
  same Turn, for example fix the build and flash again.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import sys
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol

import httpx

from tidblink_server import (
    ClientToolCallContext,
    ClientToolHandler,
    NotifiedFile,
    OrganizationClient,
    Session,
    SessionConnection,
)


class Device(Protocol):
    """Your channel to the device."""

    async def flash(self, *, url: str, byte_size: int) -> str:
        """Have the device download the image from ``url`` and flash it; return its answer, raise on failure.

        It must stop when its task is cancelled, as happens at the call's
        60-second deadline. When flashing takes longer, answer that it started
        and report the result in a later message.
        """
        ...


async def connect_device_flasher(
    session: Session,
    device: Device,
    *,
    on_file: Callable[[NotifiedFile], Awaitable[None] | None] | None = None,
) -> SessionConnection:
    """Connect the Session with the ``flash_device`` Client Tool; ``close()`` it when the device goes offline."""

    async def flash_device(args: dict[str, Any], context: ClientToolCallContext) -> str:
        ticket = await session.files.create_download_ticket(args["path"], version=args["version"])
        return await device.flash(url=ticket["url"], byte_size=ticket["byteSize"])

    return await session.connect(
        tools={
            "flash_device": ClientToolHandler(
                description="Flash a firmware image onto the device. Call notify_file first and pass its path and version.",
                parameters={
                    "type": "object",
                    "properties": {"path": {"type": "string"}, "version": {"type": "string"}},
                    "required": ["path", "version"],
                    "additionalProperties": False,
                },
                run=flash_device,
            )
        },
        on_file=on_file,
    )


class DownloadingDevice:
    """A stand-in device that downloads the image from the ticket's URL, without an API Key, and passes it to ``save``.

    Use your own device channel instead.
    """

    def __init__(self, save: Callable[[bytes], object], *, http_client: httpx.AsyncClient | None = None) -> None:
        self._save = save
        self._http_client = http_client

    async def flash(self, *, url: str, byte_size: int) -> str:
        if self._http_client is None:
            async with httpx.AsyncClient() as client:
                response = await client.get(url)
        else:
            response = await self._http_client.get(url)
        if not response.is_success:
            raise RuntimeError(f"The device's download failed with HTTP {response.status_code}")
        if len(response.content) != byte_size:
            raise RuntimeError("The device's download was incomplete")
        saved = self._save(response.content)
        if inspect.isawaitable(saved):
            await saved
        return f"Flashed {byte_size} bytes"


async def _main() -> int:
    try:
        firmware_path = os.environ.get("FIRMWARE_PATH")
        if not firmware_path:
            raise ValueError("Set FIRMWARE_PATH to where the stand-in device saves the image")
        text = os.environ.get("MESSAGE_TEXT")
        if not text:
            raise ValueError("Set MESSAGE_TEXT to the message that asks the Agent to build and flash")
        async with OrganizationClient(
            base_url=os.environ.get("AGENT_SERVICE_URL"),  # type: ignore[arg-type]
            api_key=os.environ.get("ORGANIZATION_API_KEY"),  # type: ignore[arg-type]
        ) as organization:
            session = organization.user(os.environ.get("END_USER_ID")).session(os.environ.get("SESSION_ID"))  # type: ignore[arg-type]
            conn = await connect_device_flasher(
                session,
                DownloadingDevice(Path(firmware_path).write_bytes),
                on_file=lambda file: print(
                    f"The Agent announced {file['name']} ({file['byteSize']} bytes)", file=sys.stderr
                ),
            )
            reply = await conn.send(text, client_event_id=os.environ.get("CLIENT_EVENT_ID") or str(uuid.uuid4()))
            print(reply.text)
            await conn.close()
        return 0
    except Exception as error:
        print(str(error) or "Flashing the device failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
