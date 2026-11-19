import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { setTimeout as delay } from "node:timers/promises";
import { OrganizationClient, TiDBLinkApiError } from "@tidblink/server";
import { createSessionWorkflow } from "@tidblink/server/examples/create-session.mjs";

const baseUrl = process.env.SDK_SERVICE_URL;
const organization = new OrganizationClient({
  baseUrl,
  apiKey: process.env.SDK_ORGANIZATION_KEY,
});
const otherOrganization = new OrganizationClient({
  baseUrl,
  apiKey: process.env.SDK_OTHER_ORGANIZATION_KEY,
});
const stage = (stage, userId) =>
  new Promise((resolve, reject) => {
    process.once("message", ({ value, error }) =>
      error ? reject(new Error(error)) : resolve(value),
    );
    process.send({ stage, userId });
  });
const until = async (read, matches) => {
  const end = Date.now() + 20000;
  do {
    const value = await read();
    if (matches(value)) return value;
    await delay(50);
  } while (Date.now() < end);
  throw new Error("Compiled SDK journey did not reach the expected state");
};
const unauthorized = (error) =>
  error instanceof TiDBLinkApiError && error.status === 401;
const user = await organization.createEndUser({
  externalId: `sdk_${randomUUID()}`,
  displayName: "SDK owner",
});
assert.equal(
  (await organization.getEndUser(user.userId)).userId,
  user.userId,
);
const owner = organization.user(user.userId);
const other = otherOrganization.user(user.userId);
const creation = {
  baseUrl,
  organizationApiKey: process.env.SDK_ORGANIZATION_KEY,
  userId: user.userId,
  templateId: process.env.SDK_TEMPLATE_ID,
  sessionCreationKey: randomUUID(),
};
const { session } = await createSessionWorkflow(creation);
assert.equal(
  (await createSessionWorkflow(creation)).session.id,
  session.id,
);
assert.equal(session.data.ownerUserId, user.userId);
assert.equal(session.data.template?.templateId, process.env.SDK_TEMPLATE_ID);
await session.rename("SDK Organization journey");
assert(
  (await owner.listSessions()).sessions.some(
    (item) => item.id === session.id,
  ),
);
assert(
  (await owner.listRealtimeModels()).models.some(
    (item) => item.id === "voice-a",
  ),
);
const wrong = await organization.createEndUser({
  displayName: "Wrong owner",
});
await assert.rejects(
  organization.user(wrong.userId).getSession(session.id),
  (error) => error.status === 404,
);

await stage("storage", user.userId);
const bytes = Buffer.from("SDK Organization file");
const file = await owner.files.upload({
  idempotencyKey: randomUUID(),
  originalName: "note.txt",
  contentType: "text/plain",
  bytes,
});
assert.equal(file.status, "ready");
assert.deepEqual(
  Buffer.from(await owner.files.download(file.fileId)),
  bytes,
);
assert(
  (await owner.files.list()).files.some(
    (item) => item.fileId === file.fileId,
  ),
);
await stage("text");
const command = {
  clientEventId: randomUUID(),
  text: "Read this note",
  userFileIds: [file.fileId],
};
const accepted = await session.submitMessage(command);
const result = await session.waitForResult(accepted.inputId, {
  timeoutMs: 20000,
  pollIntervalMs: 25,
});
assert.equal(result.text, "Organization SDK answer.");
assert.equal(
  (await session.reconcileInteraction(command.clientEventId)).inputId,
  accepted.inputId,
);
assert.equal(
  (await session.getTurn(result.turnId)).userFiles[0].userFileId,
  file.fileId,
);
assert(
  (await session.history()).turns.some((turn) => turn.id === result.turnId),
);
assert((await session.timeline()).entries.length > 0);

await stage("tools");
const tools = await session.getClientTools();
await session.replaceClientTools(
  [
    {
      type: "function",
      name: "lookup_order",
      description: "Get order",
      parameters: {
        type: "object",
        properties: {},
        additionalProperties: false,
      },
    },
  ],
  { ifMatch: tools.etag },
);
// The compiled backend serves Backend Connections: the tool runs on the connection.
const events = [];
const conn = await session.connect({
  tools: {
    lookup_order: {
      description: "Get order",
      parameters: { type: "object", properties: {}, additionalProperties: false },
      run: async () => "SDK_ORDER_SHIPPED",
    },
  },
  onEvent: (event) => events.push(event.type),
});
const toolReply = await conn.send("Look up my order", { timeoutMs: 20000 });
assert.equal(toolReply.text, "Order shipped.");
assert(events.includes("agent.session.turn.requires_action"));
assert(events.includes("agent.session.idle"));
await conn.close();
await until(
  () => session.refresh(),
  (current) => current.data.activeTurnId === null,
);
// An Organization serves a Connector over its Backend Connections, and a Session calls it.
const { serveOrdersConnector, memoryOrders } = await import(
  "@tidblink/server/examples/serve-connector.mjs"
);
const orderReads = [];
const sample = memoryOrders({
  [user.userId]: [{ orderId: "order_7", status: "SDK_CONNECTOR_RESULT" }],
});
const connectorErrors = [];
const served = await serveOrdersConnector({
  organization,
  name: "sdk_orders",
  orders: {
    listOrders: sample.listOrders,
    getOrder: async (userId, orderId, options) => {
      orderReads.push({ userId, orderId });
      return sample.getOrder(userId, orderId, options);
    },
  },
  onError: (error) => connectorErrors.push(error),
});
assert.match(served.connectorId, /^conn_/u);
const connector = await organization.connectors.get(served.connectorId);
assert.equal(connector.transport, "backend_connection");
assert.equal(connector.status, "active");
assert.equal(connector.observation, null);
// Another Key of the same Organization reads the same Connector.
assert(
  (await otherOrganization.connectors.list()).some(
    (item) => item.connectorId === served.connectorId,
  ),
);
const connectorTemplateId = await stage("connector", served.connectorId);
const connectorSession = await owner.createSession({
  templateId: connectorTemplateId,
  idempotencyKey: randomUUID(),
});
assert.equal(
  (
    await connectorSession.turn(
      { clientEventId: randomUUID(), text: "Where is order 7?" },
      { timeoutMs: 20000, pollIntervalMs: 25 },
    )
  ).text,
  "Connector order shipped.",
);
assert.deepEqual(orderReads, [{ userId: user.userId, orderId: "order_7" }]);
// No process serves the Connector: its call is not performed and the Turn goes on.
await served.close();
assert.deepEqual(await served.closed, { reason: "closed" });
await until(
  () => connectorSession.refresh(),
  (current) => current.data.activeTurnId === null,
);
await stage("connector_offline");
assert.equal(
  (
    await connectorSession.turn(
      { clientEventId: randomUUID(), text: "And order 8?" },
      { timeoutMs: 20000, pollIntervalMs: 25 },
    )
  ).text,
  "Connector unavailable.",
);
assert.equal(orderReads.length, 1);
assert.deepEqual(connectorErrors, []);
await until(
  () => connectorSession.refresh(),
  (current) => current.data.activeTurnId === null,
);

await stage("audio");
const wav = Buffer.alloc(364);
wav.write("RIFF", 0);
wav.writeUInt32LE(356, 4);
wav.write("WAVEfmt ", 8);
wav.writeUInt32LE(16, 16);
wav.writeUInt16LE(1, 20);
wav.writeUInt16LE(1, 22);
wav.writeUInt32LE(16000, 24);
wav.writeUInt32LE(32000, 28);
wav.writeUInt16LE(2, 32);
wav.writeUInt16LE(16, 34);
wav.write("data", 36);
wav.writeUInt32LE(320, 40);
assert.equal(
  (
    await session.audio({
      filename: "voice.wav",
      contentType: "audio/wav",
      bytes: wav,
    })
  ).text,
  "SDK audio answer.",
);
await until(
  () => session.refresh(),
  (current) => current.data.activeTurnId === null,
);


await until(
  () => session.refresh(),
  (current) => current.data.activeTurnId === null,
);
const realtime = await session.openRealtimeConnection();
assert.equal(realtime.realtimeConnection.status, "connecting");
assert((await session.createRealtimeConnectToken()).connectToken);
assert.equal(
  (await session.getRealtimeConnection()).id,
  realtime.realtimeConnection.id,
);
await stage("pause");
const inFlight = await session.submitMessage({
  clientEventId: randomUUID(),
  text: "Finish accepted work",
});
const freshKey = await stage("rotate");
await assert.rejects(owner.listSessions(), unauthorized);
const freshOrganization = new OrganizationClient({
  baseUrl,
  apiKey: freshKey,
});
const fresh = freshOrganization.user(user.userId);
const recoveredSession = fresh.session(session.id);
assert.equal(
  (
    await recoveredSession.waitForResult(inFlight.inputId, {
      timeoutMs: 20000,
      pollIntervalMs: 25,
    })
  ).text,
  "Accepted work survived rotation.",
);
assert.equal((await other.getSession(session.id)).id, session.id);
assert.equal((await recoveredSession.getRealtimeConnection()).status, "ended");
const reopened = await recoveredSession.openRealtimeConnection();
assert.notEqual(
  reopened.realtimeConnection.id,
  realtime.realtimeConnection.id,
);
await recoveredSession.endRealtimeConnection();
assert.deepEqual(
  Buffer.from(await fresh.files.download(file.fileId)),
  bytes,
);
const now = new Date();
await freshOrganization.getUserUsageDetails(user.userId, {
  from: new Date(now.getTime() - 3600000).toISOString(),
  to: now.toISOString(),
});
await stage("revoke");
await assert.rejects(fresh.listSessions(), unauthorized);
assert.equal((await other.getSession(session.id)).id, session.id);
console.log(
  "Packed Organization consumer passed: provisioning, discovery, creation replay, ownership, file transfer/attachment, text/history, Client Tools on a Backend Connection, a served Connector called and then unavailable, audio, Realtime HTTP, and rotation/revocation recovery.",
);
await organization.close();
process.disconnect();
