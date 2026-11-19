import assert from "node:assert/strict";
import { setTimeout as delay } from "node:timers/promises";
import { after, test } from "node:test";
import {
  BackendConnectionClosedError,
  BackendRequestError,
  OrganizationClient,
  ServedConnector,
  TiDBLinkError,
} from "../dist/index.js";
import * as sdk from "../dist/index.js";
import { BackendConnectionPool } from "../dist/backend-pool.js";
import { serveConnector } from "../dist/connector-serve.js";
import { memoryOrders, serveOrdersConnector } from "../examples/serve-connector.mjs";
import { backendServer } from "./support/backend-server.mjs";

const apiKey = "ti_org_connect_" + "x".repeat(43);
const baseUrl = "https://tidb-link.test";
const connectorId = "conn_00000000-0000-0000-0000-000000000001";
const json = (value, status = 200) =>
  new Response(JSON.stringify(value), {
    status,
    headers: { "content-type": "application/json" },
  });

const servers = [];
const pools = [];
const clients = [];
after(async () => {
  await Promise.all([...pools, ...clients].map((owner) => owner.close()));
  await Promise.all(servers.map((server) => server.close()));
});

const record = {
  connectorId,
  organizationId: "org_1",
  displayName: "orders",
  endpointUrl: "",
  endpointVersion: 1,
  transport: "backend_connection",
  status: "active",
  connectorVersion: 1,
  createdAt: "2026-11-19T00:00:00.000Z",
  updatedAt: "2026-11-19T00:00:00.000Z",
  observation: null,
};

/**
 * A core stand-in whose connections record every client message in
 * `peer.log` and answer through `respond(message, peer, index)`; by default
 * it serves Connectors, claims calls and accepts results.
 */
const start = async ({ respond, ...options } = {}) => {
  let count = 0;
  const server = await backendServer({
    ...options,
    onConnection: (peer) => {
      const index = count++;
      peer.log = [];
      options.onConnection?.(peer, index);
      void (async () => {
        for (;;) {
          const message = await peer.next();
          peer.log.push(message);
          const answer =
            (await respond?.(message, peer, index)) ??
            {
              "connector.serve": { result: { connectorId } },
              "connector.withdraw": { result: {} },
              "session.attach": { result: { generation: 1, cursor: "0" } },
              "session.detach": { result: {} },
              "tool.claim": { result: { claimed: true } },
              "tool.result": { result: { accepted: true } },
            }[message.method];
          if (answer && answer !== "silent")
            peer.send({ jsonrpc: "2.0", id: message.id, ...answer });
        }
      })();
    },
  });
  servers.push(server);
  return server;
};
const poolFor = (server, options = {}) => {
  const pool = new BackendConnectionPool({ baseUrl: server.baseUrl, apiKey, idleCloseMs: 50, ...options });
  pools.push(pool);
  return pool;
};
const until = async (predicate, timeoutMs = 5000) => {
  const end = Date.now() + timeoutMs;
  while (!predicate()) {
    if (Date.now() > end) throw new Error("condition not reached");
    await delay(10);
  }
};
const methodOf = (peer, method) => peer.log.filter((message) => message.method === method);
const call = (callId, name, args = {}, overrides = {}) => ({
  jsonrpc: "2.0",
  method: "tool.call",
  params: {
    callId,
    sessionId: "sess_9",
    userId: "user_9",
    turnId: "turn_9",
    name,
    arguments: args,
    claimBy: new Date(Date.now() + 5000).toISOString(),
    deadlineAt: new Date(Date.now() + 60_000).toISOString(),
    connector: { id: connectorId, name: "orders" },
    ...overrides,
  },
});
const lookup = (runs = []) => ({
  get_order: {
    description: "Read one of the user's orders",
    parameters: { type: "object", properties: { orderId: { type: "string" } }, required: ["orderId"] },
    run: async (args, context) => {
      runs.push({ args, context });
      return `Order ${args.orderId} shipped`;
    },
  },
});

test("Connectors are read, and registration, callbacks and verification are gone", async () => {
  const calls = [];
  const organization = new OrganizationClient({
    baseUrl,
    apiKey,
    fetch: async (input, init = {}) => {
      const url = new URL(String(input));
      calls.push(`${init.method ?? "GET"} ${url.pathname.replace("/api/v1/org", "")}`);
      return url.pathname.endsWith("/connectors") ? json({ connectors: [record] }) : json(record);
    },
  });
  clients.push(organization);
  const { connectors } = organization;
  assert.deepEqual(await connectors.list(), [record]);
  assert.equal((await connectors.get(connectorId)).transport, "backend_connection");
  assert.deepEqual(calls, ["GET /connectors", `GET /connectors/${connectorId}`]);
  for (const method of ["register", "update", "activate", "deactivate"])
    assert.equal(connectors[method], undefined, method);
  for (const name of [
    "ConnectorKeySet",
    "ConnectorAuthError",
    "verifyConnectorCallback",
    "readConnectorCallContext",
    "CONNECTOR_CALLBACK_ISSUER",
    "CONNECTOR_CALLBACK_AUDIENCE",
    "CONNECTOR_CALL_CONTEXT_KEY",
  ])
    assert.equal(sdk[name], undefined, name);
});

test("serveConnector checks names, functions and parameters before connecting", async () => {
  const server = await start();
  const pool = poolFor(server);
  const run = async () => "";
  const tool = (parameters = { type: "object", properties: {} }) => ({ description: "A function", parameters, run });
  for (const [options, label] of [
    [{ name: "Orders", tools: {} }, "uppercase name"],
    [{ name: "orders_", tools: {} }, "trailing underscore"],
    [{ name: "my__orders", tools: {} }, "double underscore"],
    [{ name: "o".repeat(30), tools: {} }, "30 characters"],
    [{ name: "orders", tools: { "get-order": tool() } }, "function name with a dash"],
    [{ name: "orders", tools: { get_order: { ...tool(), run: undefined } } }, "no run"],
    [{ name: "orders", tools: { get_order: tool({ properties: {} }) } }, "parameters without type object"],
    [{ name: "orders", tools: { get_order: tool({ type: "object", properties: { async: { type: "boolean" } } }) } }, "defines async"],
    [{ name: "orders", tools: { get_order: tool({ type: "object", required: ["async"] }) } }, "requires async"],
    [{ name: "orders", tools: { get_order: { ...tool(), description: "" } } }, "empty description"],
    [{ name: "orders", tools: Object.fromEntries(Array.from({ length: 65 }, (_, i) => [`f${i}`, tool()])) }, "65 functions"],
    [{ name: "orders", tools: [] }, "tools array"],
    [{ name: "orders", tools: {}, extra: true }, "unknown option"],
  ])
    await assert.rejects(serveConnector(pool, options), TypeError, label);
  assert.equal(server.peers.length, 0);
  // The longest names are accepted.
  const served = await serveConnector(pool, { name: "o".repeat(29), tools: { ["f".repeat(29)]: tool() } });
  assert.equal(served.connectorId, connectorId);
});

test("a served Connector runs a call from any Session on the connection that delivered it", async () => {
  const server = await start();
  const organization = new OrganizationClient({ baseUrl: server.baseUrl, apiKey });
  clients.push(organization);
  const runs = [];
  const served = await organization.serveConnector({ name: "orders", tools: lookup(runs) });
  assert(served instanceof ServedConnector);
  assert.equal(served.name, "orders");
  assert.equal(served.connectorId, connectorId);
  const peer = server.peers[0];
  assert.deepEqual(methodOf(peer, "connector.serve")[0].params, {
    name: "orders",
    tools: [
      {
        type: "function",
        name: "get_order",
        description: "Read one of the user's orders",
        parameters: { type: "object", properties: { orderId: { type: "string" } }, required: ["orderId"] },
      },
    ],
  });
  // No Session is attached: the call is routed by its Connector.
  peer.send(call("call_1", "get_order", { orderId: "o_7" }));
  await until(() => methodOf(peer, "tool.result").length === 1);
  assert.deepEqual(methodOf(peer, "tool.claim")[0].params, { callId: "call_1" });
  assert.deepEqual(methodOf(peer, "tool.result")[0].params, { callId: "call_1", output: "Order o_7 shipped" });
  assert.equal(runs.length, 1);
  const { context } = runs[0];
  assert.deepEqual(
    { ...context, deadlineAt: undefined, signal: undefined },
    {
      callId: "call_1",
      sessionId: "sess_9",
      userId: "user_9",
      turnId: "turn_9",
      name: "get_order",
      deadlineAt: undefined,
      signal: undefined,
      connector: { id: connectorId, name: "orders" },
    },
  );
  assert(context.deadlineAt instanceof Date);
  // A call for a Connector this process does not serve is left unclaimed.
  peer.send(call("call_2", "get_order", { orderId: "o_8" }, { connector: { id: "conn_x", name: "billing" } }));
  await delay(50);
  assert.equal(methodOf(peer, "tool.claim").length, 1);
});

test("refused claims never run, duplicates run once, and unknown functions and deadlines become errors", async () => {
  const server = await start({
    respond: (message) =>
      message.method === "tool.claim" && message.params.callId.startsWith("refused")
        ? { result: { claimed: false, reason: message.params.callId.slice(8) } }
        : undefined,
  });
  const pool = poolFor(server);
  const runs = [];
  await serveConnector(pool, {
    name: "orders",
    tools: {
      ...lookup(runs),
      slow: {
        description: "Never finishes",
        parameters: { type: "object" },
        run: (_args, { signal }) => new Promise((_, reject) => signal.addEventListener("abort", () => reject(signal.reason))),
      },
    },
  });
  const peer = server.peers[0];
  for (const reason of ["call_not_found", "call_settled", "claim_window_closed", "superseded"])
    peer.send(call(`refused_${reason}`, "get_order", { orderId: "o_1" }));
  peer.send(call("call_1", "get_order", { orderId: "o_1" }));
  peer.send(call("call_1", "get_order", { orderId: "o_1" }));
  peer.send(call("call_2", "list_everything"));
  peer.send(call("call_3", "slow", {}, { deadlineAt: new Date(Date.now() + 100).toISOString() }));
  await until(() => methodOf(peer, "tool.result").length === 3);
  await delay(50);
  assert.equal(runs.length, 1);
  assert.equal(methodOf(peer, "tool.claim").length, 7);
  const results = Object.fromEntries(methodOf(peer, "tool.result").map((m) => [m.params.callId, m.params]));
  assert.deepEqual(Object.keys(results).sort(), ["call_1", "call_2", "call_3"]);
  assert.deepEqual(results.call_2.error, {
    message: "Connector orders has no function list_everything in this Application Server",
  });
  assert.deepEqual(results.call_3.error, { message: "The tool did not finish before its deadline" });
});

test("a Connector is declared on every connection, including ones opened later", async () => {
  const server = await start({ helloParams: { maxSessions: 1 } });
  const pool = poolFor(server);
  const session = (sessionId) => ({
    sessionId,
    attachParams: () => ({ userId: "user_1", sessionId }),
    attached() {},
    notify() {},
    ended() {},
  });
  await pool.attach(session("s1"));
  await pool.attach(session("s2"));
  assert.equal(pool.connections, 2);
  await serveConnector(pool, { name: "orders", tools: lookup() });
  await until(() => server.peers.every((peer) => methodOf(peer, "connector.serve").length === 1));
  await pool.attach(session("s3"));
  assert.equal(pool.connections, 3);
  await until(() => methodOf(server.peers[2], "connector.serve").length === 1);
  // A call delivered on the second connection is claimed there.
  server.peers[1].send(call("call_1", "get_order", { orderId: "o_1" }));
  await until(() => methodOf(server.peers[1], "tool.claim").length === 1);
  assert.equal(methodOf(server.peers[0], "tool.claim").length, 0);
});

test("serving keeps one connection open; close() withdraws it and idle connections close again", async () => {
  const server = await start();
  const pool = poolFor(server);
  const served = await serveConnector(pool, { name: "orders", tools: lookup() });
  await delay(200);
  assert.equal(pool.connections, 1);
  assert.equal(server.peers.length, 1);
  await served.close();
  assert.deepEqual(methodOf(server.peers[0], "connector.withdraw")[0].params, { name: "orders" });
  assert.deepEqual(await served.closed, { reason: "closed" });
  assert.equal((await server.peers[0].closed).code, 1000);
  assert.equal(pool.connections, 0);
  // The name is free again.
  await serveConnector(pool, { name: "orders", tools: lookup() });
});

test("a lost or draining connection declares the Connector again on a new one", async () => {
  const server = await start();
  const pool = poolFor(server);
  const served = await serveConnector(pool, { name: "orders", tools: lookup() });
  server.peers[0].destroy();
  const second = await server.connection(1);
  await until(() => methodOf(second, "connector.serve").length === 1);
  second.send({ jsonrpc: "2.0", method: "connection.drain", params: { closeAfterMs: 5000 } });
  const third = await server.connection(2);
  await until(() => methodOf(third, "connector.serve").length === 1);
  assert.equal((await second.closed).code, 1000);
  assert.equal(pool.connections, 1);
  third.send(call("call_1", "get_order", { orderId: "o_1" }));
  await until(() => methodOf(third, "tool.result").length === 1);
  let settled = false;
  served.closed.then(() => (settled = true));
  await delay(20);
  assert.equal(settled, false);
});

test("a duplicate name, a refused declaration and a revoked Key end serving", async () => {
  let revoked = false;
  const server = await start({
    onConnection: (peer) => {
      if (revoked) peer.close(4001, "authentication failed");
    },
    respond: (message) =>
      message.method === "connector.serve" && message.params.name === "invalid"
        ? { error: { code: -32602, message: "Invalid params" } }
        : undefined,
  });
  const pool = poolFor(server);
  const errors = [];
  const served = await serveConnector(pool, { name: "orders", tools: lookup(), onError: (e) => errors.push(e) });
  await assert.rejects(serveConnector(pool, { name: "orders", tools: lookup() }), TiDBLinkError);
  await assert.rejects(
    serveConnector(pool, { name: "invalid", tools: lookup() }),
    (error) => error instanceof BackendRequestError && error.code === -32602,
  );
  // The refused name is not served: it can be tried again.
  await assert.rejects(serveConnector(pool, { name: "invalid", tools: lookup() }), BackendRequestError);
  revoked = true;
  server.peers[0].close(4001, "revoked");
  const end = await served.closed;
  assert.equal(end.reason, "error");
  assert(end.error instanceof BackendConnectionClosedError);
  assert.equal(end.error.code, 4001);
  assert.deepEqual(errors, []);
});

test("closing the OrganizationClient ends serving and refuses new Connectors", async () => {
  const server = await start();
  const organization = new OrganizationClient({ baseUrl: server.baseUrl, apiKey });
  const served = await organization.serveConnector({ name: "orders", tools: lookup() });
  await organization.close();
  assert.deepEqual(await served.closed, { reason: "closed" });
  await assert.rejects(organization.serveConnector({ name: "orders", tools: lookup() }), TiDBLinkError);
});

test("the packaged example answers with the calling user's orders only", async () => {
  const server = await start();
  const organization = new OrganizationClient({ baseUrl: server.baseUrl, apiKey });
  clients.push(organization);
  const orders = memoryOrders({
    user_9: [{ orderId: "o_1", status: "shipped" }],
    user_8: [{ orderId: "o_2", status: "open" }],
  });
  const served = await serveOrdersConnector({ organization, orders });
  assert.equal(served.connectorId, connectorId);
  const peer = server.peers[0];
  assert.deepEqual(
    methodOf(peer, "connector.serve")[0].params.tools.map((tool) => tool.name),
    ["list_orders", "get_order"],
  );
  peer.send(call("call_1", "list_orders"));
  peer.send(call("call_2", "get_order", { orderId: "o_2" }));
  await until(() => methodOf(peer, "tool.result").length === 2);
  const results = Object.fromEntries(methodOf(peer, "tool.result").map((m) => [m.params.callId, m.params]));
  assert.deepEqual(JSON.parse(results.call_1.output), [{ orderId: "o_1", status: "shipped" }]);
  assert.deepEqual(results.call_2.error, { message: "Order o_2 was not found" });
});

test("a transient refusal of a further declaration is retried on the same connection", async () => {
  let refusals = 1;
  const server = await start({
    helloParams: { maxSessions: 1 },
    respond: (message, _peer, index) =>
      message.method === "connector.serve" && index === 1 && refusals-- > 0
        ? { error: { code: -32603, message: "Internal error" } }
        : undefined,
  });
  const pool = poolFor(server);
  const errors = [];
  await serveConnector(pool, { name: "orders", tools: lookup(), onError: (e) => errors.push(e) });
  await pool.attach({
    sessionId: "s1",
    attachParams: () => ({ userId: "user_1", sessionId: "s1" }),
    attached() {},
    notify() {},
    ended() {},
  });
  await pool.attach({
    sessionId: "s2",
    attachParams: () => ({ userId: "user_1", sessionId: "s2" }),
    attached() {},
    notify() {},
    ended() {},
  });
  await until(() => server.peers[1] && methodOf(server.peers[1], "connector.serve").length === 2);
  assert.deepEqual(errors, []);
});
