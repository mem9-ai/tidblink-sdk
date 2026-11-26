import { randomUUID } from "node:crypto";
import { writeFile } from "node:fs/promises";
import { pathToFileURL } from "node:url";
import { OrganizationClient, TurnWaitTimeoutError } from "@tidblink/server";

/**
 * Let the Agent flash firmware it built onto a device within the same Turn.
 *
 * The Agent writes the image in its Session's Drive, announces it with
 * `notify_file`, then calls the `flash_device` Client Tool with the announced
 * `path` and `version`. Tell the model so in the Template's instructions.
 *
 * - `onFile` receives each File Notification, for example to show the user
 *   what was built. It asks for no reply.
 * - `flash_device` mints a download ticket pinned to the announced version and
 *   hands its URL to the device, which downloads the image without an API Key
 *   within 60 seconds. If the Agent changed the file after announcing it,
 *   minting fails with `DriveFileVersionConflictError`, whose message the
 *   model sees.
 * - The device's answer is the tool's result, so the model can react in the
 *   same Turn, for example fix the build and flash again.
 *
 * `device.flash({ url, byteSize }, { signal })` is your channel to the device:
 * it resolves with the device's answer and rejects on failure. A call has a
 * 60-second deadline, at which `signal` aborts; when flashing takes longer,
 * answer that it started and report the result in a later message.
 *
 * Returns the Session connection; `close()` it when the device goes offline.
 * The device must stay connected until the Turn ends: a `flash_device` call
 * made while no connection executes the Session fails as not delivered.
 */
export function connectDeviceFlasher({ session, device, onFile }) {
  return session.connect({
    tools: {
      flash_device: {
        description:
          "Flash a firmware image onto the device. Call notify_file first and pass its path and version.",
        parameters: {
          type: "object",
          properties: { path: { type: "string" }, version: { type: "string" } },
          required: ["path", "version"],
          additionalProperties: false,
        },
        run: async ({ path, version }, { signal }) => {
          const ticket = await session.files.createDownloadTicket(path, { version, signal });
          return device.flash({ url: ticket.url, byteSize: ticket.byteSize }, { signal });
        },
      },
    },
    ...(onFile ? { onFile } : {}),
  });
}

/**
 * Send `text`, which asks the Agent to build and flash, and keep the device
 * connected until the Turn ends, so that the Agent's `flash_device` calls
 * reach it; then disconnect. Returns the Turn's result.
 *
 * A build can take minutes, so the wait, `timeoutMs`, defaults to 10 minutes
 * instead of `send()`'s 60 seconds. When it ends first, the call throws
 * `TurnWaitTimeoutError` and disconnects; the Turn goes on without the device.
 */
export async function flashDeviceWorkflow({
  session,
  device,
  text,
  clientEventId,
  timeoutMs = 10 * 60_000,
  onFile,
}) {
  const conn = await connectDeviceFlasher({ session, device, onFile });
  try {
    return await conn.send(text, { clientEventId, timeoutMs });
  } finally {
    await conn.close();
  }
}

/**
 * A stand-in device that downloads the image from the ticket's URL, without
 * an API Key, and passes it to `save`. Use your own device channel instead.
 */
export function downloadingDevice(save) {
  return {
    flash: async ({ url, byteSize }, { signal }) => {
      const response = await fetch(url, { signal });
      if (!response.ok) throw new Error(`The device's download failed with HTTP ${response.status}`);
      const image = new Uint8Array(await response.arrayBuffer());
      if (image.byteLength !== byteSize) throw new Error("The device's download was incomplete");
      await save(image);
      return `Flashed ${byteSize} bytes`;
    },
  };
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  let organization;
  // How long the device stays connected for the Turn: 10 minutes unless set.
  const seconds = process.env.TURN_TIMEOUT_SECONDS || "600";
  try {
    const { FIRMWARE_PATH: firmwarePath, MESSAGE_TEXT: text } = process.env;
    if (!firmwarePath) throw new Error("Set FIRMWARE_PATH to where the stand-in device saves the image");
    if (!text) throw new Error("Set MESSAGE_TEXT to the message that asks the Agent to build and flash");
    if (!/^[1-9][0-9]{0,5}$/.test(seconds))
      throw new Error("Set TURN_TIMEOUT_SECONDS to a whole number of seconds, or unset it to wait 10 minutes");
    organization = new OrganizationClient({
      baseUrl: process.env.AGENT_SERVICE_URL,
      apiKey: process.env.ORGANIZATION_API_KEY,
    });
    const reply = await flashDeviceWorkflow({
      session: organization.user(process.env.END_USER_ID).session(process.env.SESSION_ID),
      device: downloadingDevice((image) => writeFile(firmwarePath, image)),
      text,
      clientEventId: process.env.CLIENT_EVENT_ID || randomUUID(),
      timeoutMs: Number(seconds) * 1000,
      onFile: (file) => console.error(`The Agent announced ${file.name} (${file.byteSize} bytes)`),
    });
    console.log(reply.text);
  } catch (error) {
    console.error(
      error instanceof TurnWaitTimeoutError
        ? `The Turn did not finish within ${seconds} seconds. It goes on without the device, ` +
            "whose flash_device calls now fail; set TURN_TIMEOUT_SECONDS to wait longer"
        : error instanceof Error
          ? error.message
          : "Flashing the device failed",
    );
    process.exitCode = 1;
  } finally {
    await organization?.close();
  }
}
