import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import { setTimeout as delay } from "node:timers/promises";
import { after, test } from "node:test";
import { OrganizationClient } from "../dist/index.js";
import {
  connectDeviceFlasher,
  downloadingDevice,
  flashDeviceWorkflow,
} from "../examples/flash-device.mjs";
import { backendServer } from "./support/backend-server.mjs";

const apiKey = "ti_org_flash_" + "x".repeat(43);
const sessionPath = "/api/v1/org/users/user_1/sessions/sess_1";
const at = "2026-11-26T00:00:00.000Z";
const done = "2026-11-26T00:05:00.000Z";
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
 * `respond(message, peer)` may answer a message first, such as `session.send`;
 * REST reads the message's settled Turn.
 */
const start = async (drive, { respond } = {}) => {
  const http = [];
  const server = await backendServer({
    onConnection: (peer) => {
      peer.log = [];
      void (async () => {
        for (;;) {
          const message = await peer.next();
          peer.log.push(message);
          const result =
            respond?.(message, peer) ??
            {
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
      if (req.method === "GET" && req.url === `${sessionPath}/interactions/msg_1`)
        return json(res, { interaction: receipt({ appliedAt: at, result: { status: "succeeded", completedAt: done } }) });
      if (req.method === "GET" && req.url === `${sessionPath}/turns/turn_1`) return json(res, { turn: savedTurn });
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
const turnEvent = (cursor, type, event, payload) => ({
  jsonrpc: "2.0",
  method: "session.event",
  params: {
    sessionId: "sess_1",
    cursor,
    event: {
      type,
      sessionId: "sess_1",
      turnId: "turn_1",
      createdAt: at,
      data: { event, turnId: "turn_1", sessionId: "sess_1", seq: Number(cursor), createdAt: at, payload },
    },
  },
});
const notified = (cursor, file) =>
  turnEvent(cursor, "agent.session.turn.item.added", "operation_step", {
    operationId: "op_1",
    status: "succeeded",
    label: "Notified file",
    file,
  });
const finished = (cursor) =>
  turnEvent(cursor, "agent.session.turn.completed", "turn_finished", { status: "succeeded" });
const idle = (cursor) => ({
  jsonrpc: "2.0",
  method: "session.event",
  params: {
    sessionId: "sess_1",
    cursor,
    event: {
      type: "agent.session.idle",
      sessionId: "sess_1",
      turnId: "turn_1",
      createdAt: done,
      data: { id: `idle_${cursor}`, turnId: "turn_1", createdAt: done },
    },
  },
});
const text = "Build the firmware and flash it";
const receipt = (overrides = {}) => ({
  inputId: "msg_1",
  sequence: 1,
  kind: "message",
  mode: "started",
  turnId: "turn_1",
  message: { id: "um_1", role: "user", content: text, createdAt: at },
  createdAt: at,
  appliedAt: null,
  rejection: null,
  result: { status: "running", completedAt: null },
  ...overrides,
});
const savedTurn = {
  id: "turn_1",
  sessionId: "sess_1",
  status: "succeeded",
  billingTag: null,
  userMessage: { id: "um_1", role: "user", content: text, sessionId: "sess_1", turnId: "turn_1", createdAt: at },
  operations: [],
  assistantMessage: {
    id: "am_1",
    role: "assistant",
    content: "Built and flashed the firmware.",
    sessionId: "sess_1",
    turnId: "turn_1",
    createdAt: done,
  },
  createdAt: at,
  completedAt: done,
};
const script = fileURLToPath(new URL("../examples/flash-device.mjs", import.meta.url));

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

test("flashDeviceWorkflow keeps the device connected until the Turn ends, so a flash_device call late in the build reaches it", async () => {
  const drive = new Map([[firmware.path, { version: "v1", bytes: Buffer.from("firmware v1") }]]);
  const { server } = await start(drive, {
    respond: (message, peer) => {
      if (message.method === "session.send") {
        // The Agent builds for a while, announces the image, then calls the tool.
        setTimeout(() => {
          peer.send(notified("11", firmware));
          peer.send(toolCall("call_1", { path: firmware.path, version: firmware.version }));
        }, 200);
        return { interaction: receipt(), replayed: false };
      }
      if (message.method === "tool.result")
        setTimeout(() => {
          peer.send(finished("12"));
          peer.send(idle("13"));
        }, 20);
      return undefined;
    },
  });
  const announced = [];
  const flashed = [];
  const reply = await flashDeviceWorkflow({
    session: sessionOf(server),
    device: downloadingDevice((image) => flashed.push(Buffer.from(image).toString())),
    text,
    clientEventId: "msg_1",
    timeoutMs: 5000,
    onFile: (file) => announced.push(file),
  });
  assert.equal(reply.text, "Built and flashed the firmware.");
  assert.deepEqual(announced, [firmware]);
  assert.deepEqual(flashed, ["firmware v1"]);
  const peer = server.peers[0];
  assert.deepEqual(methodOf(peer, "tool.result")[0].params, { callId: "call_1", output: "Flashed 11 bytes" });
  // Connected for the whole Turn, disconnected once it ended.
  assert.deepEqual(
    peer.log.map((message) => message.method),
    ["session.attach", "session.send", "tool.claim", "tool.result", "session.detach"],
  );
});

test("the flash-device script waits TURN_TIMEOUT_SECONDS for the Turn, then says how to wait longer", async () => {
  const { server } = await start(new Map(), {
    // The Turn is still building when the wait ends.
    respond: (message) =>
      message.method === "session.send" ? { interaction: receipt(), replayed: false } : undefined,
  });
  await assert.rejects(
    promisify(execFile)(process.execPath, [script], {
      env: {
        ...process.env,
        AGENT_SERVICE_URL: server.baseUrl,
        ORGANIZATION_API_KEY: apiKey,
        END_USER_ID: "user_1",
        SESSION_ID: "sess_1",
        MESSAGE_TEXT: text,
        CLIENT_EVENT_ID: "msg_1",
        FIRMWARE_PATH: join(tmpdir(), "flash-device-unused.bin"),
        TURN_TIMEOUT_SECONDS: "1",
      },
      timeout: 20000,
    }),
    (error) =>
      error.code === 1 &&
      /did not finish within 1 second/.test(error.stderr) &&
      /TURN_TIMEOUT_SECONDS/.test(error.stderr),
  );
  assert.equal(methodOf(server.peers[0], "session.send").length, 1);
});

test("the flash-device script names a missing or invalid setting before connecting", async () => {
  const { FIRMWARE_PATH, MESSAGE_TEXT, TURN_TIMEOUT_SECONDS, ...env } = process.env;
  await assert.rejects(
    promisify(execFile)(process.execPath, [script], { env }),
    (error) => error.code === 1 && /Set FIRMWARE_PATH/.test(error.stderr) && error.stdout === "",
  );
  await assert.rejects(
    promisify(execFile)(process.execPath, [script], {
      env: { ...env, FIRMWARE_PATH: "device.bin", MESSAGE_TEXT: text, TURN_TIMEOUT_SECONDS: "ten minutes" },
    }),
    (error) => error.code === 1 && /Set TURN_TIMEOUT_SECONDS/.test(error.stderr) && error.stdout === "",
  );
});
