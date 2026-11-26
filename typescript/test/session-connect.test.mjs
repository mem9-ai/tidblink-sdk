import assert from "node:assert/strict";
import { setTimeout as delay } from "node:timers/promises";
import { after, test } from "node:test";
import {
  BackendConnectionClosedError,
  BackendRequestError,
  OrganizationClient,
  SessionConnection,
  TiDBLinkError,
} from "../dist/index.js";
import { backendServer } from "./support/backend-server.mjs";

const apiKey = "ti_org_connect_" + "x".repeat(43);
const servers = [];
const clients = [];
after(async () => {
  await Promise.all(clients.map((client) => client.close()));
  await Promise.all(servers.map((server) => server.close()));
});

/** A core stand-in: records messages in `peer.log`, answers attach and detach. */
const start = async ({ respond, ...options } = {}) => {
  let count = 0;
  const server = await backendServer({
    ...options,
    onConnection: (peer) => {
      const index = count++;
      peer.log = [];
      options.onConnection?.(peer);
      void (async () => {
        for (;;) {
          const message = await peer.next();
          peer.log.push(message);
          const answer =
            (await respond?.(message, peer, index)) ??
            (message.method === "session.attach"
              ? { result: { generation: 4, cursor: "10" } }
              : message.method === "session.detach"
                ? { result: {} }
                : undefined);
          if (answer && answer !== "silent")
            peer.send({ jsonrpc: "2.0", id: message.id, ...answer });
        }
      })();
    },
  });
  servers.push(server);
  return server;
};
const clientFor = (server) => {
  const client = new OrganizationClient({ baseUrl: server.baseUrl, apiKey });
  clients.push(client);
  return client;
};
const sessionOf = (server, sessionId = "sess_1") =>
  clientFor(server).user("user_1").session(sessionId);
const until = async (predicate, timeoutMs = 5000) => {
  const end = Date.now() + timeoutMs;
  while (!predicate()) {
    if (Date.now() > end) throw new Error("condition not reached");
    await delay(10);
  }
};
const createdAt = "2026-11-13T00:00:00.000Z";
const eventFrame = (cursor, type, data = {}, sessionId = "sess_1") => ({
  jsonrpc: "2.0",
  method: "session.event",
  params: {
    sessionId,
    cursor,
    event: { type, sessionId, turnId: null, createdAt, data },
  },
});
const realtime = (type, extra) => ({
  id: `rt_${type}`,
  type,
  realtimeConnectionId: "rtc_1",
  text: type === "input_transcript" ? "too bright" : "Dimmed it.",
  turnId: null,
  createdAt,
  ...extra,
});
const lampTools = {
  set_light: {
    description: "Turn the lamp on or off",
    parameters: { type: "object", properties: { on: { type: "boolean" } } },
    run: () => "ok",
  },
};

test("connect() attaches the Session for execution and declares its Client Tools", async () => {
  const server = await start();
  const conn = await sessionOf(server).connect({ tools: lampTools });
  assert(conn instanceof SessionConnection);
  assert.equal(conn.sessionId, "sess_1");
  assert.equal(conn.userId, "user_1");
  assert.equal(conn.generation, 4);
  assert.equal(conn.cursor, "10");
  const peer = server.peers[0];
  assert.equal(peer.request.headers.authorization, `Bearer ${apiKey}`);
  assert.equal(peer.request.url, "/api/v1/org/backend/ws");
  assert.deepEqual(peer.log[0].params, {
    userId: "user_1",
    sessionId: "sess_1",
    tools: [
      {
        type: "function",
        name: "set_light",
        description: "Turn the lamp on or off",
        parameters: { type: "object", properties: { on: { type: "boolean" } } },
      },
    ],
  });
  await conn.close();
  assert.deepEqual(await conn.closed, { reason: "closed" });
  assert.deepEqual(peer.log.at(-1).params, { sessionId: "sess_1" });
});

test("observing connections and resume cursors are sent as given; invalid options never dispatch", async () => {
  const server = await start();
  const session = sessionOf(server);
  await session.connect({ observe: true, cursor: "42" });
  assert.deepEqual(server.peers[0].log[0].params, {
    userId: "user_1",
    sessionId: "sess_1",
    kind: "observing",
    cursor: "42",
  });
  for (const options of [
    { observe: true, tools: lampTools },
    { tools: { set_light: { description: "x", parameters: {} } } },
    { tools: { "bad name": { ...lampTools.set_light } } },
    { tools: [lampTools.set_light] },
    { cursor: "abc" },
    { observe: true, maxBufferedBytes: 0 },
    { observe: true, maxBufferedBytes: 1.5 },
    { maxBufferedBytes: 1024 },
    { onEvent: "nope" },
    { onFile: {} },
    { unknown: true },
  ])
    await assert.rejects(session.connect(options), TypeError);
  assert.equal(server.peers[0].log.length, 1);
});

test("events arrive in order through their callbacks, and the cursor advances after them", async () => {
  const server = await start();
  const seen = [];
  const errors = [];
  const conn = await sessionOf(server).connect({
    onEvent: async (event) => {
      await delay(5);
      seen.push(["event", event.type, event.cursor]);
      if (event.cursor === "13") throw new Error("application failure");
    },
    onTranscript: (transcript) => seen.push(["transcript", transcript.text]),
    onResponse: (response) => seen.push(["response", response.text, response.kind]),
    onCursor: (cursor) => seen.push(["cursor", cursor]),
    onError: (error) => errors.push(error),
  });
  const peer = server.peers[0];
  peer.send(eventFrame("11", "agent.session.realtime.input_transcript.completed", realtime("input_transcript")));
  peer.send(
    eventFrame(
      "12",
      "agent.session.realtime.response.completed",
      realtime("response", { responseId: "r1", kind: "chat", interrupted: false }),
    ),
  );
  peer.send(eventFrame("13", "agent.session.future.event", { anything: true }));
  peer.send({ jsonrpc: "2.0", method: "session.event", params: { sessionId: "sess_1", cursor: "14", event: { type: "broken" } } });
  peer.send(eventFrame("15", "agent.session.template_switch.succeeded", {}));
  await until(() => conn.cursor === "15");
  assert.deepEqual(seen, [
    ["event", "agent.session.realtime.input_transcript.completed", "11"],
    ["transcript", "too bright"],
    ["cursor", "11"],
    ["event", "agent.session.realtime.response.completed", "12"],
    ["response", "Dimmed it.", "chat"],
    ["cursor", "12"],
    ["event", "agent.session.future.event", "13"],
    ["cursor", "13"],
    ["event", "agent.session.template_switch.succeeded", "15"],
    ["cursor", "15"],
  ]);
  assert.equal(errors.length, 2);
  assert.equal(errors[0].message, "application failure");
  assert(errors[1] instanceof TiDBLinkError);
});

test("onFile receives each file a File Notification announces, after onEvent and before the cursor", async () => {
  const server = await start();
  const seen = [];
  const errors = [];
  const conn = await sessionOf(server).connect({
    onEvent: (event) => seen.push(["event", event.cursor]),
    onFile: async (file) => {
      await delay(5);
      seen.push(["file", file]);
      if (file.name === "broken.bin") throw new Error("application failure");
    },
    onCursor: (cursor) => seen.push(["cursor", cursor]),
    onError: (error) => errors.push(error),
  });
  const firmware = {
    path: "build/firmware.bin",
    name: "firmware.bin",
    version: "v1AAAAAAAAAAAAAAAAAAAA",
    byteSize: 4,
    updatedAt: createdAt,
  };
  const step = (cursor, payload) => ({
    ...eventFrame(cursor, "agent.session.turn.item.added", {
      event: "operation_step",
      turnId: "turn_1",
      sessionId: "sess_1",
      seq: Number(cursor),
      createdAt,
      payload: {
        operationId: `op_${cursor}`,
        label: "Notifying file",
        category: "tool_call",
        ...payload,
      },
    }),
  });
  for (const frame of [
    step("11", { status: "running" }),
    step("12", { status: "succeeded", label: "Completed notifying file", file: firmware }),
    step("13", { status: "succeeded", file: { ...firmware, byteSize: "4" } }),
    step("14", { status: "succeeded", file: { ...firmware, name: "broken.bin" } }),
  ]) {
    frame.params.event.turnId = "turn_1";
    server.peers[0].send(frame);
  }
  await until(() => conn.cursor === "14");
  assert.deepEqual(seen, [
    ["event", "11"],
    ["cursor", "11"],
    ["event", "12"],
    ["file", firmware],
    ["cursor", "12"],
    // A malformed file reaches onEvent only.
    ["event", "13"],
    ["cursor", "13"],
    ["event", "14"],
    ["file", { ...firmware, name: "broken.bin" }],
    ["cursor", "14"],
  ]);
  assert.equal(errors.length, 1);
  assert.equal(errors[0].message, "application failure");
});

test("after a lost connection the Session re-attaches from the last processed event and skips replays", async () => {
  const server = await start();
  const cursors = [];
  const conn = await sessionOf(server).connect({
    tools: lampTools,
    onCursor: (cursor) => cursors.push(cursor),
  });
  const first = server.peers[0];
  first.send(eventFrame("11", "agent.session.turn.started"));
  await until(() => conn.cursor === "11");
  first.destroy();
  const second = await server.connection(1);
  await until(() => second.log.length === 1);
  assert.deepEqual(second.log[0].params, {
    userId: "user_1",
    sessionId: "sess_1",
    tools: first.log[0].params.tools,
    cursor: "11",
  });
  // The core replays after the cursor; a duplicate from a race is skipped.
  second.send(eventFrame("11", "agent.session.turn.started"));
  second.send(eventFrame("12", "agent.session.turn.completed"));
  await until(() => conn.cursor === "12");
  assert.deepEqual(cursors, ["11", "12"]);
  assert.equal(conn.generation, 4);
});

test("a connection taken over elsewhere ends as superseded and stops delivering", async () => {
  const server = await start();
  const seen = [];
  const conn = await sessionOf(server).connect({ onEvent: (event) => seen.push(event.cursor) });
  const peer = server.peers[0];
  peer.send({ jsonrpc: "2.0", method: "session.detached", params: { sessionId: "sess_1", reason: "superseded" } });
  assert.deepEqual(await conn.closed, { reason: "superseded" });
  peer.send(eventFrame("11", "agent.session.turn.started"));
  await delay(50);
  assert.deepEqual(seen, []);
  await conn.close();
  assert.equal(peer.log.filter((m) => m.method === "session.detach").length, 0);
});

test("one connection per Session per client; refusals and a revoked Key surface as errors", async () => {
  const server = await start({
    respond: (message) =>
      message.params?.sessionId === "missing"
        ? { error: { code: -32000, message: "Refused", data: { reason: "session_not_found" } } }
        : undefined,
  });
  const client = clientFor(server);
  const conn = await client.user("user_1").session("sess_1").connect();
  await assert.rejects(
    client.user("user_1").session("sess_1").connect(),
    /already connected/,
  );
  await assert.rejects(
    client.user("user_1").session("missing").connect(),
    (error) => error instanceof BackendRequestError && error.reason === "session_not_found",
  );
  server.peers[0].close(4001, "revoked");
  const end = await conn.closed;
  assert.equal(end.reason, "error");
  assert(end.error instanceof BackendConnectionClosedError);
  assert.equal(end.error.retryable, false);
});

test("OrganizationClient.close() ends its connections as closed and refuses new ones", async () => {
  const server = await start();
  const client = clientFor(server);
  const conn = await client.user("user_1").session("sess_1").connect();
  await client.close();
  assert.deepEqual(await conn.closed, { reason: "closed" });
  assert(server.peers[0].log.some((m) => m.method === "session.detach"));
  await assert.rejects(client.user("user_1").session("sess_2").connect(), /closed/);
});

test("a connection whose callbacks fall behind drops events, then has the core send them again", async () => {
  // The core stand-in keeps every event it sent, and an attach with a cursor replays those after it.
  const sent = [];
  const server = await start({
    respond: (message, peer) => {
      if (message.method !== "session.attach") return undefined;
      const after = BigInt(message.params.cursor ?? "10");
      setImmediate(() => {
        for (const frame of sent)
          if (BigInt(frame.params.cursor) > after) peer.send(frame);
      });
      return { result: { generation: 4, cursor: String(sent.length + 10) } };
    },
  });
  const delivered = [];
  let release;
  let gate = new Promise((resolve) => (release = resolve));
  const frame = (n) => eventFrame(String(10 + n), "agent.session.turn.progress", { text: "x".repeat(200) });
  const size = JSON.stringify(frame(1)).length;
  const conn = await sessionOf(server).connect({
    observe: true,
    maxBufferedBytes: size * 3,
    onEvent: async (event) => {
      delivered.push(event.cursor);
      await gate;
    },
  });
  const peer = server.peers[0];
  for (let n = 1; n <= 10; n += 1) {
    sent.push(frame(n));
    peer.send(frame(n));
  }
  await until(() => delivered.length === 1);
  await delay(50);
  // Only what fits is waiting; nothing was attached again yet.
  assert.equal(delivered.length, 1);
  assert.equal(peer.log.filter((message) => message.method === "session.attach").length, 1);
  release();
  gate = Promise.resolve();
  await until(() => delivered.length === 10);
  assert.deepEqual(delivered, Array.from({ length: 10 }, (_, n) => String(11 + n)));
  // Each re-attach resumes after the last event the callbacks completed; the replay itself may
  // outrun them again.
  const attaches = peer.log.filter((message) => message.method === "session.attach");
  assert(attaches.length >= 2);
  assert.equal(attaches[1].params.cursor, "13");
  for (const attach of attaches.slice(1)) assert(Number(attach.params.cursor) >= 13);
  assert.equal(conn.cursor, "20");
  await conn.close();
});

test("events that arrive together with the attach answer are delivered after connect() returns", async () => {
  const server = await start({
    respond: (message, peer) => {
      if (message.method !== "session.attach") return undefined;
      // The answer and the replay leave in one write, as a fast core sends them.
      peer.send({ jsonrpc: "2.0", id: message.id, result: { generation: 4, cursor: "13" } });
      for (const cursor of ["11", "12", "13"])
        peer.send(eventFrame(cursor, "agent.session.turn.progress"));
      return "silent";
    },
  });
  const delivered = [];
  // The callbacks run only once connect() has handed over the connection.
  const conn = await sessionOf(server).connect({
    observe: true,
    cursor: "10",
    onEvent: (event) => {
      delivered.push([event.cursor, conn.sessionId]);
    },
  });
  await until(() => delivered.length === 3);
  assert.deepEqual(delivered, [
    ["11", "sess_1"],
    ["12", "sess_1"],
    ["13", "sess_1"],
  ]);
});

test("an executing connection never attaches again to catch up", async () => {
  const server = await start();
  const delivered = [];
  let release;
  const gate = new Promise((resolve) => (release = resolve));
  await sessionOf(server).connect({
    tools: lampTools,
    onEvent: async (event) => {
      delivered.push(event.cursor);
      await gate;
    },
  });
  const peer = server.peers[0];
  const big = "x".repeat(4 * 1024 * 1024);
  for (let n = 1; n <= 4; n += 1)
    peer.send(eventFrame(String(10 + n), "agent.session.turn.progress", { text: big }));
  await until(() => delivered.length === 1);
  release();
  await until(() => delivered.length === 4);
  assert.equal(peer.log.filter((message) => message.method === "session.attach").length, 1);
});

test("a catch-up the core refuses because the Session is gone ends the connection as deleted", async () => {
  let attaches = 0;
  const server = await start({
    respond: (message) => {
      if (message.method !== "session.attach") return undefined;
      attaches += 1;
      return attaches === 1
        ? { result: { generation: 4, cursor: "10" } }
        : { error: { code: -32000, message: "gone", data: { reason: "session_not_found" } } };
    },
  });
  let release;
  const gate = new Promise((resolve) => (release = resolve));
  const frame = (n) => eventFrame(String(10 + n), "agent.session.turn.progress", { text: "y".repeat(200) });
  const conn = await sessionOf(server).connect({
    observe: true,
    maxBufferedBytes: JSON.stringify(frame(1)).length * 2,
    onEvent: () => gate,
  });
  const peer = server.peers[0];
  for (let n = 1; n <= 5; n += 1) peer.send(frame(n));
  await delay(50);
  release();
  assert.deepEqual(await conn.closed, { reason: "session_deleted" });
  assert.equal(attaches, 2);
  await until(() => peer.log.at(-1)?.method === "session.detach");
});
