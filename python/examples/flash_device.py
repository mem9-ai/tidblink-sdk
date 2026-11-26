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
- ``flash_device_workflow`` sends the message and keeps the device connected
  until the Turn ends, waiting up to 10 minutes because a build can take
  minutes. The script reads that wait from ``TURN_TIMEOUT_SECONDS``.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import re
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
    TurnResult,
    TurnWaitTimeoutError,
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
    """Connect the Session with the ``flash_device`` Client Tool; ``close()`` it when the device goes offline.

    The device must stay connected until the Turn ends: a ``flash_device``
    call made while no connection executes the Session fails as not delivered.
    """

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


async def flash_device_workflow(
    session: Session,
    device: Device,
    text: str,
    *,
    client_event_id: str | None = None,
    timeout: float = 600,
    on_file: Callable[[NotifiedFile], Awaitable[None] | None] | None = None,
) -> TurnResult:
    """Send ``text``, which asks the Agent to build and flash, and keep the device connected until the Turn ends.

    The Agent's ``flash_device`` calls reach the device until then; the
    connection closes afterwards. A build can take minutes, so ``timeout``
    defaults to 10 minutes instead of ``send()``'s 60 seconds. When it ends
    first, ``TurnWaitTimeoutError`` is raised and the device disconnects; the
    Turn goes on without it.
    """
    conn = await connect_device_flasher(session, device, on_file=on_file)
    try:
        return await conn.send(text, client_event_id=client_event_id, timeout=timeout)
    finally:
        await conn.close()


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
    # How long the device stays connected for the Turn: 10 minutes unless set.
    seconds = os.environ.get("TURN_TIMEOUT_SECONDS") or "600"
    try:
        firmware_path = os.environ.get("FIRMWARE_PATH")
        if not firmware_path:
            raise ValueError("Set FIRMWARE_PATH to where the stand-in device saves the image")
        text = os.environ.get("MESSAGE_TEXT")
        if not text:
            raise ValueError("Set MESSAGE_TEXT to the message that asks the Agent to build and flash")
        if not re.fullmatch(r"[1-9][0-9]{0,5}", seconds):
            raise ValueError("Set TURN_TIMEOUT_SECONDS to a whole number of seconds, or unset it to wait 10 minutes")
        async with OrganizationClient(
            base_url=os.environ.get("AGENT_SERVICE_URL"),  # type: ignore[arg-type]
            api_key=os.environ.get("ORGANIZATION_API_KEY"),  # type: ignore[arg-type]
        ) as organization:
            session = organization.user(os.environ.get("END_USER_ID")).session(os.environ.get("SESSION_ID"))  # type: ignore[arg-type]
            reply = await flash_device_workflow(
                session,
                DownloadingDevice(Path(firmware_path).write_bytes),
                text,
                client_event_id=os.environ.get("CLIENT_EVENT_ID") or str(uuid.uuid4()),
                timeout=int(seconds),
                on_file=lambda file: print(
                    f"The Agent announced {file['name']} ({file['byteSize']} bytes)", file=sys.stderr
                ),
            )
            print(reply.text)
        return 0
    except TurnWaitTimeoutError:
        print(
            f"The Turn did not finish within {seconds} seconds. It goes on without the device, "
            "whose flash_device calls now fail; set TURN_TIMEOUT_SECONDS to wait longer",
            file=sys.stderr,
        )
        return 1
    except Exception as error:
        print(str(error) or "Flashing the device failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
