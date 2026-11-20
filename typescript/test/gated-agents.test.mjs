import assert from "node:assert/strict";
import { readdir, readFile } from "node:fs/promises";
import { createServer } from "node:http";
import { after, test } from "node:test";
import * as root from "../dist/index.js";
import {
  ConflictError,
  OrganizationClient,
  OutcomeUnknownError,
  Session,
  TiDBLinkApiError,
} from "../dist/index.js";
import * as gated from "../dist/gated/index.js";
import { Agents, FeatureNotEnabledError, agents } from "../dist/gated/index.js";

const key = "ti_org_key_" + "x".repeat(43);
const servers = [];
after(async () =>
  Promise.all(
    servers.map((server) => new Promise((resolve) => server.close(resolve))),
  ),
);

/** Serve `responses` in order and record every request. */
const serve = async (...responses) => {
  const calls = [];
  const server = createServer(async (req, res) => {
    let body = "";
    for await (const chunk of req) body += chunk;
    calls.push({
      method: req.method,
      url: req.url,
      headers: req.headers,
      body: body ? JSON.parse(body) : undefined,
    });
    const next = responses.shift() ?? { status: 500, body: {} };
    if (next === "destroy") return res.destroy();
    res
      .writeHead(next.status, {
        "content-type": "application/json",
        ...next.headers,
      })
      .end(next.body === undefined ? undefined : JSON.stringify(next.body));
  });
  servers.push(server);
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const baseUrl = `http://127.0.0.1:${server.address().port}`;
  const user = new OrganizationClient({ baseUrl, apiKey: key }).user("user_1");
  return { calls, user };
};

const wireAgent = {
  agentId: "agent_1",
  userId: "user_1",
  name: "Support",
  sessionDefaults: {
    agentTemplateId: "template_1",
    additionalInstructions: "Use metric units.",
    sandbox: { profileId: null, workingDirectory: "agents/support" },
  },
  createdAt: "2026-11-20T00:00:00.000Z",
  updatedAt: "2026-11-20T00:00:00.000Z",
};
const sdkDefaults = {
  templateId: "template_1",
  additionalInstructions: "Use metric units.",
  sandbox: { profileId: null, workingDirectory: "agents/support" },
};
const sdkAgent = { ...wireAgent, sessionDefaults: sdkDefaults };
const agentsPath = "/api/v1/org/users/user_1/agents";
const notEnabled = {
  status: 403,
  headers: { "x-request-id": "req_refused" },
  body: {
    error: {
      code: "agent_preview_not_enabled",
      message: "The Agent preview is not enabled for this Organization.",
    },
  },
};

test("agents(user) sends no request and accepts only an EndUserClient", async () => {
  const { calls, user } = await serve();
  assert(agents(user) instanceof Agents);
  assert.equal(calls.length, 0);
  for (const value of [undefined, {}, "user_1", { files: {} }])
    assert.throws(() => agents(value), TypeError);
});

test("create sends the SDK's templateId as agentTemplateId and returns the Agent with its ETag", async () => {
  const { calls, user } = await serve("destroy", {
    status: 201,
    headers: { etag: '"agent_1.r1"' },
    body: { agent: wireAgent },
  });
  const agent = await agents(user).create({
    name: "Support",
    sessionDefaults: sdkDefaults,
    idempotencyKey: "agent-create-1",
  });
  assert.deepEqual(agent, { ...sdkAgent, etag: '"agent_1.r1"' });
  // The creation key makes the retry safe.
  assert.equal(calls.length, 2);
  for (const call of calls) {
    assert.equal(call.method, "POST");
    assert.equal(call.url, agentsPath);
    assert.equal(call.headers["idempotency-key"], "agent-create-1");
    assert.equal(call.headers.authorization, `Bearer ${key}`);
    assert.deepEqual(call.body, {
      name: "Support",
      sessionDefaults: wireAgent.sessionDefaults,
    });
  }
  assert.equal(calls[0].headers["x-request-id"], calls[1].headers["x-request-id"]);
});

test("create checks the Template, creation key and option names locally", async () => {
  const { calls, user } = await serve();
  const valid = {
    name: "Support",
    sessionDefaults: { templateId: "template_1" },
    idempotencyKey: "agent-create-1",
  };
  const invalid = [
    { ...valid, idempotencyKey: undefined },
    { ...valid, idempotencyKey: "bad key" },
    { ...valid, sessionDefaults: {} },
    { ...valid, sessionDefaults: { templateId: " template_1" } },
    { ...valid, sessionDefaults: { templateId: "template_1", tools: [] } },
    { ...valid, sessionDefaults: { agentTemplateId: "template_1" } },
    { ...valid, name: " " },
    { ...valid, extra: true },
  ];
  for (const input of invalid)
    await assert.rejects(agents(user).create(input), TypeError);
  assert.equal(calls.length, 0);
});

test("an uncertain creation is reported as an unknown outcome", async () => {
  const { user } = await serve({ status: 201, body: { agent: wireAgent } });
  await assert.rejects(
    agents(user).create({
      name: "Support",
      sessionDefaults: sdkDefaults,
      idempotencyKey: "agent-create-1",
    }),
    OutcomeUnknownError,
  );
});

test("list pages Agents without ETags", async () => {
  const { calls, user } = await serve({
    status: 200,
    body: { agents: [wireAgent], hasMore: true, nextCursor: "cursor_2" },
  });
  const page = await agents(user).list({ limit: 1, cursor: "cursor_1" });
  assert.deepEqual(page, {
    agents: [sdkAgent],
    hasMore: true,
    nextCursor: "cursor_2",
  });
  assert.equal(calls[0].url, `${agentsPath}?limit=1&cursor=cursor_1`);
  await assert.rejects(agents(user).list({ sort: "name" }), TypeError);
});

test("get returns the Agent with the ETag update needs", async () => {
  const { calls, user } = await serve(
    { status: 200, headers: { etag: '"agent/1.r3"' }, body: { agent: wireAgent } },
    { status: 200, body: { agent: wireAgent } },
  );
  const agent = await agents(user).get("agent/1");
  assert.equal(agent.etag, '"agent/1.r3"');
  assert.deepEqual(agent.sessionDefaults, sdkDefaults);
  assert.equal(calls[0].method, "GET");
  assert.equal(calls[0].url, `${agentsPath}/agent%2F1`);
  await assert.rejects(agents(user).get("agent_1"), /omitted a required ETag/);
  await assert.rejects(agents(user).get(""), TypeError);
});

test("update sends If-Match, only the given fields and replaces the defaults whole", async () => {
  const updated = { ...wireAgent, name: "Help" };
  const { calls, user } = await serve(
    { status: 200, headers: { etag: '"agent_1.r2"' }, body: { agent: updated } },
    { status: 200, headers: { etag: '"agent_1.r3"' }, body: { agent: updated } },
    {
      status: 412,
      body: { error: { code: "precondition_failed", message: "If-Match is stale" } },
    },
  );
  const agent = await agents(user).update(
    "agent_1",
    { name: "Help" },
    { ifMatch: '"agent_1.r1"' },
  );
  assert.equal(agent.name, "Help");
  assert.equal(agent.etag, '"agent_1.r2"');
  assert.equal(calls[0].method, "PATCH");
  assert.equal(calls[0].url, `${agentsPath}/agent_1`);
  assert.equal(calls[0].headers["if-match"], '"agent_1.r1"');
  assert.deepEqual(calls[0].body, { name: "Help" });

  await agents(user).update(
    "agent_1",
    { sessionDefaults: { templateId: "template_2", model: "model_1" } },
    { ifMatch: '"agent_1.r2"' },
  );
  assert.deepEqual(calls[1].body, {
    sessionDefaults: { agentTemplateId: "template_2", model: "model_1" },
  });

  await assert.rejects(
    agents(user).update("agent_1", { name: "Again" }, { ifMatch: '"agent_1.r1"' }),
    ConflictError,
  );
  // A PATCH is not repeated automatically.
  assert.equal(calls.length, 3);

  for (const [input, options] of [
    [{ name: "Help" }, {}],
    [{ name: "Help" }, { ifMatch: "" }],
    [{ name: "Help", userId: "user_2" }, { ifMatch: '"x"' }],
    [{ sessionDefaults: { model: "model_1" } }, { ifMatch: '"x"' }],
  ])
    await assert.rejects(agents(user).update("agent_1", input, options), TypeError);
  assert.equal(calls.length, 3);
});

test("delete removes the Agent and reports a lost answer as an unknown outcome", async () => {
  const { calls, user } = await serve({ status: 204 }, "destroy");
  assert.equal(await agents(user).delete("agent_1"), undefined);
  assert.equal(calls[0].method, "DELETE");
  assert.equal(calls[0].url, `${agentsPath}/agent_1`);
  await assert.rejects(agents(user).delete("agent_1"), OutcomeUnknownError);
  assert.equal(calls.length, 2);
});

test("createSession returns an ordinary Session bound to the End User", async () => {
  const record = { sessionId: "session_1", model: "model_1" };
  const { calls, user } = await serve(
    "destroy",
    {
      status: 201,
      headers: { etag: '"model-1"' },
      body: { agentId: "agent_1", session: record },
    },
    { status: 200, headers: { etag: '"model-1"' }, body: { session: record } },
  );
  const tools = [
    {
      type: "function",
      name: "lookup_order",
      description: "Read an order",
      parameters: { type: "object" },
    },
  ];
  const session = await agents(user).createSession("agent_1", {
    idempotencyKey: "agent-session-1",
    model: "model_1",
    billingTag: { key: "agent", value: "support" },
    tools,
  });
  assert(session instanceof Session);
  assert.equal(session.id, "session_1");
  assert.deepEqual(session.data, record);
  assert.equal(calls.length, 2);
  for (const call of calls.slice(0, 2)) {
    assert.equal(call.method, "POST");
    assert.equal(call.url, `${agentsPath}/agent_1/sessions`);
    assert.equal(call.headers["idempotency-key"], "agent-session-1");
    assert.deepEqual(call.body, {
      model: "model_1",
      billingTag: { key: "agent", value: "support" },
      tools,
    });
  }
  // It is the ordinary Session of this End User from here on.
  await session.refresh();
  assert.equal(calls[2].url, "/api/v1/org/users/user_1/sessions/session_1");
});

test("createSession accepts only the per-Session choices and reports lost receipts as unknown", async () => {
  const { calls, user } = await serve(
    { status: 201, body: { agentId: "agent_1", session: { sessionId: "session_1" } } },
    { status: 201, headers: { etag: '"model-1"' }, body: { agentId: "agent_1" } },
  );
  for (const options of [
    {},
    { idempotencyKey: "agent-session-1", templateId: "template_1" },
    { idempotencyKey: "agent-session-1", additionalInstructions: "Hi" },
    { idempotencyKey: "agent-session-1", tools: [{ name: "x" }] },
  ])
    await assert.rejects(agents(user).createSession("agent_1", options), TypeError);
  assert.equal(calls.length, 0);
  for (let attempt = 0; attempt < 2; attempt += 1)
    await assert.rejects(
      agents(user).createSession("agent_1", { idempotencyKey: "agent-session-1" }),
      OutcomeUnknownError,
    );
});

test("listSessions returns the ordinary Session page", async () => {
  const { calls, user } = await serve({
    status: 200,
    body: {
      sessions: [{ sessionId: "session_2" }, { sessionId: "session_1" }],
      hasMore: false,
      nextCursor: null,
    },
  });
  const page = await agents(user).listSessions("agent_1", { limit: 2 });
  assert.deepEqual(
    page.sessions.map((session) => session.id),
    ["session_2", "session_1"],
  );
  assert(page.sessions.every((session) => session instanceof Session));
  assert.equal(page.hasMore, false);
  assert.equal(calls[0].url, `${agentsPath}/agent_1/sessions?limit=2`);
});

test("an Organization not enabled for Agents gets FeatureNotEnabledError from every method", async () => {
  const { calls, user } = await serve(...Array(7).fill(notEnabled));
  const all = agents(user);
  const invocations = [
    () =>
      all.create({
        name: "Support",
        sessionDefaults: sdkDefaults,
        idempotencyKey: "agent-create-1",
      }),
    () => all.list(),
    () => all.get("agent_1"),
    () => all.update("agent_1", { name: "Help" }, { ifMatch: '"agent_1.r1"' }),
    () => all.delete("agent_1"),
    () => all.createSession("agent_1", { idempotencyKey: "agent-session-1" }),
    () => all.listSessions("agent_1"),
  ];
  for (const invoke of invocations)
    await assert.rejects(invoke(), (error) => {
      assert(error instanceof FeatureNotEnabledError);
      assert(error instanceof TiDBLinkApiError);
      assert.equal(error.name, "FeatureNotEnabledError");
      assert.equal(error.feature, "agents");
      assert.equal(error.status, 403);
      assert.equal(error.code, "agent_preview_not_enabled");
      assert.equal(error.requestId, "req_refused");
      assert.match(error.message, /not enabled for this Organization/);
      assert.doesNotMatch(error.message, /preview/i);
      return true;
    });
  // A refusal is final, so nothing is repeated.
  assert.equal(calls.length, invocations.length);
});

test("other refusals keep their ordinary errors", async () => {
  const { user } = await serve(
    {
      status: 403,
      body: { error: { code: "auth_kind_forbidden", message: "Organization key required" } },
    },
    {
      status: 404,
      body: { error: { code: "agent_not_found", message: "Agent not found" } },
    },
    {
      status: 409,
      body: {
        error: { code: "session_creation_conflict", message: "Key used with other input" },
      },
    },
  );
  const all = agents(user);
  for (const [invoke, code, ErrorClass] of [
    [() => all.list(), "auth_kind_forbidden", TiDBLinkApiError],
    [() => all.get("agent_2"), "agent_not_found", TiDBLinkApiError],
    [
      () => all.createSession("agent_1", { idempotencyKey: "agent-session-1" }),
      "session_creation_conflict",
      ConflictError,
    ],
  ])
    await assert.rejects(invoke(), (error) => {
      assert(error instanceof ErrorClass);
      assert(!(error instanceof FeatureNotEnabledError));
      assert.equal(error.code, code);
      return true;
    });
});

test("the root entry neither exports nor imports the gated entry", async () => {
  assert.deepEqual(Object.keys(gated).sort(), [
    "Agents",
    "FeatureNotEnabledError",
    "agents",
  ]);
  for (const name of Object.keys(gated)) assert(!(name in root), name);
  const dist = new URL("../dist/", import.meta.url);
  const files = (await readdir(dist, { recursive: true })).filter(
    (file) => /\.(?:js|d\.ts)$/.test(file) && !file.startsWith("gated/"),
  );
  assert(files.includes("index.js"));
  for (const file of files)
    assert.doesNotMatch(
      await readFile(new URL(file, dist), "utf8"),
      /from "\.\/gated\/|import\("\.\/gated\//,
      file,
    );
  const manifest = JSON.parse(
    await readFile(new URL("../package.json", import.meta.url), "utf8"),
  );
  assert.deepEqual(manifest.exports["./gated"], {
    types: "./dist/gated/index.d.ts",
    import: "./dist/gated/index.js",
  });
});
