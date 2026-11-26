import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import { setTimeout as delay } from "node:timers/promises";
import { after, test } from "node:test";
import { OrganizationClient } from "../dist/index.js";
import { connectDeviceFlasher, downloadingDevice } from "../examples/flash-device.mjs";
import { backendServer } from "./support/backend-server.mjs";

const apiKey = "ti_org_flash_" + "x".repeat(43);
const sessionPath = "/api/v1/org/users/user_1/sessions/sess_1";
const at = "2026-11-26T00:00:00.000Z";
const servers = [];
const clients = [];
after(async () => {
  await Promise.all(clients.map((client) => client.close()));
  await Promise.all(servers.map((server) => server.close()));
});

const json = (res, value, status = 200) =>
  res.writeHead(status, { "content-type": "application/json" }).end(JSON.stringify(value));
const methodOf = (peer, method) => peer.log.filter((message) => message.method === method);
const until = async (predicate, timeoutMs = 5000) => {
  const end = Date.now() + timeoutMs;
  while (!predicate()) {
    if (Date.now() > end) throw new Error("condition not reached");
    await delay(10);
  }
};

/**
 * A core stand-in: answers attach and Client Tool claims and results over the
 * Backend Connection, mints tickets for the files in `drive`, and redeems them.
 */
const start = async (drive) => {
  const http = [];
  const server = await backendServer({
    onConnection: (peer) => {
      peer.log = [];
      void (async () => {
        for (;;) {
          const message = await peer.next();
          peer.log.push(message);
          const result = {
            "session.attach": { generation: 1, cursor: "10" },
            "session.detach": {},
            "tool.claim": { claimed: true },
            "tool.result": { accepted: true },
          }[message.method];
          if (result) peer.send({ jsonrpc: "2.0", id: message.id, result });
        }
      })();
    },
    http: async (req, res) => {
      let body = "";
      for await (const chunk of req) body += chunk;
      http.push({
        method: req.method,
        url: req.url,
        authorization: req.headers.authorization,
        ...(body ? { body: JSON.parse(body) } : {}),
      });
      if (req.method === "POST" && req.url === `${sessionPath}/files/download-tickets`) {
        const { path, version } = JSON.parse(body);
        const file = drive.get(path);
        if (!file) return json(res, { error: { code: "drive_file_not_found", message: "Not found" } }, 404);
        if (file.version !== version)
          return json(
            res,
            { error: { code: "drive_file_version_conflict", message: "The file changed after it was announced" } },
            409,
          );
        return json(
          res,
          {
            url: `/api/v1/drive-downloads/ticket.${encodeURIComponent(path)}`,
            expiresAt: "2099-01-01T00:01:00.000Z",
            byteSize: file.bytes.length,
          },
          201,
        );
      }
      if (req.method === "GET" && req.url.startsWith("/api/v1/drive-downloads/ticket.")) {
        const file = drive.get(decodeURIComponent(req.url.slice("/api/v1/drive-downloads/ticket.".length)));
        return res.writeHead(200, { "content-type": "application/octet-stream" }).end(file.bytes);
      }
      res.writeHead(404).end();
    },
  });
  servers.push(server);
  return { server, http };
};
const sessionOf = (server) => {
  const client = new OrganizationClient({ baseUrl: server.baseUrl, apiKey });
  clients.push(client);
  return client.user("user_1").session("sess_1");
};
const toolCall = (callId, args) => ({
  jsonrpc: "2.0",
  method: "tool.call",
  params: {
    callId,
    sessionId: "sess_1",
    userId: "user_1",
    turnId: "turn_1",
    name: "flash_device",
    arguments: args,
    claimBy: new Date(Date.now() + 5000).toISOString(),
    deadlineAt: new Date(Date.now() + 60000).toISOString(),
  },
});
const firmware = {
  path: "builds/firmware.bin",
  name: "Firmware 1",
  version: "v1",
  byteSize: 11,
  updatedAt: at,
};
const notified = (cursor, file) => ({
  jsonrpc: "2.0",
  method: "session.event",
  params: {
    sessionId: "sess_1",
    cursor,
    event: {
      type: "agent.session.turn.item.added",
      sessionId: "sess_1",
      turnId: "turn_1",
      createdAt: at,
      data: {
        event: "operation_step",
        turnId: "turn_1",
        sessionId: "sess_1",
        seq: Number(cursor),
        createdAt: at,
        payload: { operationId: "op_1", status: "succeeded", label: "Notified file", file },
      },
    },
  },
});

test("the flash-device example announces the file and flashes the announced version through a keyless ticket", async () => {
  const drive = new Map([[firmware.path, { version: "v1", bytes: Buffer.from("firmware v1") }]]);
  const { server, http } = await start(drive);
  const announced = [];
  const flashed = [];
  const conn = await connectDeviceFlasher({
    session: sessionOf(server),
    device: downloadingDevice((image) => flashed.push(Buffer.from(image).toString())),
    onFile: (file) => announced.push(file),
  });
  const peer = server.peers[0];
  assert.deepEqual(methodOf(peer, "session.attach")[0].params.tools, [
    {
      type: "function",
      name: "flash_device",
      description:
        "Flash a firmware image onto the device. Call notify_file first and pass its path and version.",
      parameters: {
        type: "object",
        properties: { path: { type: "string" }, version: { type: "string" } },
        required: ["path", "version"],
        additionalProperties: false,
      },
    },
  ]);
  peer.send(notified("11", firmware));
  await until(() => announced.length === 1);
  assert.deepEqual(announced, [firmware]);

  peer.send(toolCall("call_1", { path: firmware.path, version: firmware.version }));
  await until(() => methodOf(peer, "tool.result").length === 1);
  assert.deepEqual(methodOf(peer, "tool.result")[0].params, {
    callId: "call_1",
    output: "Flashed 11 bytes",
  });
  assert.deepEqual(flashed, ["firmware v1"]);
  assert.deepEqual(http, [
    {
      method: "POST",
      url: `${sessionPath}/files/download-tickets`,
      authorization: `Bearer ${apiKey}`,
      body: { path: firmware.path, version: "v1" },
    },
    // The device downloads without the API Key.
    {
      method: "GET",
      url: `/api/v1/drive-downloads/ticket.${encodeURIComponent(firmware.path)}`,
      authorization: undefined,
    },
  ]);

  // The Agent changed the file after announcing it: the model learns so.
  drive.set(firmware.path, { version: "v2", bytes: Buffer.from("firmware v2") });
  peer.send(toolCall("call_2", { path: firmware.path, version: firmware.version }));
  await until(() => methodOf(peer, "tool.result").length === 2);
  assert.deepEqual(methodOf(peer, "tool.result")[1].params, {
    callId: "call_2",
    error: { message: "The file changed after it was announced" },
  });
  assert.deepEqual(flashed, ["firmware v1"]);
  await conn.close();
});

test("the flash-device script names a missing setting before connecting", async () => {
  const script = fileURLToPath(new URL("../examples/flash-device.mjs", import.meta.url));
  const { FIRMWARE_PATH, MESSAGE_TEXT, ...env } = process.env;
  await assert.rejects(
    promisify(execFile)(process.execPath, [script], { env }),
    (error) => error.code === 1 && /Set FIRMWARE_PATH/.test(error.stderr) && error.stdout === "",
  );
});
