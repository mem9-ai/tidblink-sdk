import { randomUUID } from "node:crypto";
import { writeFile } from "node:fs/promises";
import { pathToFileURL } from "node:url";
import { OrganizationClient } from "@tidblink/server";

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
  try {
    const { FIRMWARE_PATH: firmwarePath, MESSAGE_TEXT: text } = process.env;
    if (!firmwarePath) throw new Error("Set FIRMWARE_PATH to where the stand-in device saves the image");
    if (!text) throw new Error("Set MESSAGE_TEXT to the message that asks the Agent to build and flash");
    organization = new OrganizationClient({
      baseUrl: process.env.AGENT_SERVICE_URL,
      apiKey: process.env.ORGANIZATION_API_KEY,
    });
    const conn = await connectDeviceFlasher({
      session: organization.user(process.env.END_USER_ID).session(process.env.SESSION_ID),
      device: downloadingDevice((image) => writeFile(firmwarePath, image)),
      onFile: (file) => console.error(`The Agent announced ${file.name} (${file.byteSize} bytes)`),
    });
    const reply = await conn.send(text, {
      clientEventId: process.env.CLIENT_EVENT_ID || randomUUID(),
    });
    console.log(reply.text);
    await conn.close();
  } catch (error) {
    console.error(error instanceof Error ? error.message : "Flashing the device failed");
    process.exitCode = 1;
  } finally {
    await organization?.close();
  }
}
