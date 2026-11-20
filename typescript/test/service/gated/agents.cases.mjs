import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { test } from "node:test";
import {
  ConflictError,
  OrganizationClient,
  Session,
  TiDBLinkApiError,
} from "@tidblink/server";
import { FeatureNotEnabledError, agents } from "@tidblink/server/gated";

const baseUrl = process.env.SDK_SERVICE_URL;
const organization = new OrganizationClient({
  baseUrl,
  apiKey: process.env.SDK_ORGANIZATION_KEY,
});
const user = organization.user(process.env.SDK_USER_ID);
const stranger = organization.user(process.env.SDK_FOREIGN_USER_ID);
const closed = new OrganizationClient({
  baseUrl,
  apiKey: process.env.SDK_CLOSED_ORGANIZATION_KEY,
}).user(process.env.SDK_CLOSED_USER_ID);
const templateId = process.env.SDK_TEMPLATE_ID;
const secondTemplateId = process.env.SDK_SECOND_TEMPLATE_ID;

const sessionDefaults = {
  templateId,
  additionalInstructions: "Use metric units.",
  sandbox: { profileId: null, workingDirectory: "agents/support" },
  billingTag: { key: "agent", value: "support" },
};
const createAgent = (name = "Support", defaults = sessionDefaults) =>
  agents(user).create({
    name,
    sessionDefaults: defaults,
    idempotencyKey: `agent-${randomUUID()}`,
  });
const refusedWith = (code, ErrorClass = TiDBLinkApiError) => (error) => {
  assert(error instanceof ErrorClass, String(error));
  assert.equal(error.code, code);
  return true;
};

test("an Organization that is not enabled is refused before any lookup and nothing is created", async () => {
  const gated = agents(closed);
  const notEnabled = (error) => {
    assert(error instanceof FeatureNotEnabledError, String(error));
    assert(error instanceof TiDBLinkApiError);
    assert.equal(error.feature, "agents");
    assert.equal(error.status, 403);
    assert.equal(error.code, "agent_preview_not_enabled");
    assert.match(error.requestId, /\S/);
    return true;
  };
  const key = `agent-${randomUUID()}`;
  await assert.rejects(
    gated.create({ name: "Closed", sessionDefaults: { templateId: "template_unknown" }, idempotencyKey: key }),
    notEnabled,
  );
  await assert.rejects(gated.list(), notEnabled);
  await assert.rejects(gated.get("agent_unknown"), notEnabled);
  await assert.rejects(
    gated.update("agent_unknown", { name: "x" }, { ifMatch: '"agent_unknown.r1"' }),
    notEnabled,
  );
  await assert.rejects(gated.delete("agent_unknown"), notEnabled);
  await assert.rejects(
    gated.createSession("agent_unknown", { idempotencyKey: key }),
    notEnabled,
  );
  await assert.rejects(gated.listSessions("agent_unknown"), notEnabled);
  // The ordinary surface of that Organization is unaffected.
  assert.equal((await closed.listSessions()).sessions.length, 0);
});

test("Agents are created idempotently, read, listed newest first and paged", async () => {
  const key = `agent-${randomUUID()}`;
  const input = { name: "Paged first", sessionDefaults, idempotencyKey: key };
  const first = await agents(user).create(input);
  assert.match(first.agentId, /\S/);
  assert.equal(first.userId, process.env.SDK_USER_ID);
  assert.equal(first.name, "Paged first");
  assert.deepEqual(first.sessionDefaults, sessionDefaults);
  assert.match(first.etag, /\S/);
  const replay = await agents(user).create(input);
  assert.equal(replay.agentId, first.agentId);
  await assert.rejects(
    agents(user).create({ ...input, name: "Other" }),
    refusedWith("idempotency_key_reused", ConflictError),
  );

  const read = await agents(user).get(first.agentId);
  assert.deepEqual(read, first);

  const second = await createAgent("Paged second");
  const third = await createAgent("Paged third");
  const page = await agents(user).list({ limit: 2 });
  assert.deepEqual(
    page.agents.map((agent) => agent.agentId),
    [third.agentId, second.agentId],
  );
  assert.equal(page.hasMore, true);
  assert(!("etag" in page.agents[0]));
  assert.deepEqual(page.agents[0].sessionDefaults, sessionDefaults);
  const seen = [...page.agents];
  for (let cursor = page.nextCursor; cursor; ) {
    const next = await agents(user).list({ limit: 2, cursor });
    seen.push(...next.agents);
    cursor = next.nextCursor;
  }
  assert(seen.some((agent) => agent.agentId === first.agentId));
  assert.equal(new Set(seen.map((agent) => agent.agentId)).size, seen.length);
});

test("updates need the current ETag and replace the Session defaults whole; deletion keeps Sessions", async () => {
  const agent = await createAgent("Editable");
  const renamed = await agents(user).update(
    agent.agentId,
    { name: "Edited" },
    { ifMatch: agent.etag },
  );
  assert.equal(renamed.name, "Edited");
  assert.deepEqual(renamed.sessionDefaults, sessionDefaults);
  assert.notEqual(renamed.etag, agent.etag);
  await assert.rejects(
    agents(user).update(agent.agentId, { name: "Stale" }, { ifMatch: agent.etag }),
    refusedWith("precondition_failed", ConflictError),
  );

  const replaced = await agents(user).update(
    agent.agentId,
    { sessionDefaults: { templateId: secondTemplateId } },
    { ifMatch: renamed.etag },
  );
  assert.deepEqual(replaced.sessionDefaults, { templateId: secondTemplateId });
  assert.deepEqual((await agents(user).get(agent.agentId)).sessionDefaults, {
    templateId: secondTemplateId,
  });
  await assert.rejects(
    agents(user).update(
      agent.agentId,
      { sessionDefaults: { templateId: "template_unknown" } },
      { ifMatch: replaced.etag },
    ),
    refusedWith("agent_template_unavailable"),
  );

  const session = await agents(user).createSession(agent.agentId, {
    idempotencyKey: `session-${randomUUID()}`,
  });
  assert.equal(session.data.template.templateId, secondTemplateId);
  await agents(user).delete(agent.agentId);
  await assert.rejects(
    agents(user).get(agent.agentId),
    refusedWith("agent_not_found"),
  );
  await assert.rejects(
    agents(user).delete(agent.agentId),
    refusedWith("agent_not_found"),
  );
  assert.equal((await user.getSession(session.id)).data.status, "active");
});

test("another End User cannot see the Agent", async () => {
  const agent = await createAgent("Private");
  await assert.rejects(
    agents(stranger).get(agent.agentId),
    refusedWith("agent_not_found"),
  );
  await assert.rejects(
    agents(stranger).createSession(agent.agentId, { idempotencyKey: `session-${randomUUID()}` }),
    refusedWith("agent_not_found"),
  );
  assert(!(await agents(stranger).list()).agents.some((item) => item.agentId === agent.agentId));
});

test("a Session created through an Agent is an ordinary Session of the End User", async () => {
  const agent = await createAgent("Sessions");
  const key = `session-${randomUUID()}`;
  const session = await agents(user).createSession(agent.agentId, {
    idempotencyKey: key,
    billingTag: { key: "agent", value: "override" },
  });
  assert(session instanceof Session);
  assert.equal(session.data.ownerUserId, process.env.SDK_USER_ID);
  assert.equal(session.data.template.templateId, templateId);
  assert.deepEqual(session.data.billingTag, { key: "agent", value: "override" });
  // A retry returns the same Session even after the Agent changed.
  const edited = await agents(user).update(
    agent.agentId,
    { sessionDefaults: { templateId: secondTemplateId } },
    { ifMatch: agent.etag },
  );
  const retried = await agents(user).createSession(agent.agentId, {
    idempotencyKey: key,
    billingTag: { key: "agent", value: "override" },
  });
  assert.equal(retried.id, session.id);
  await assert.rejects(
    agents(user).createSession(agent.agentId, { idempotencyKey: key }),
    refusedWith("session_creation_conflict", ConflictError),
  );

  await session.rename("Through an Agent");
  const reread = await user.getSession(session.id);
  assert.equal(reread.data.name, "Through an Agent");
  assert((await user.listSessions({ limit: 200 })).sessions.some((item) => item.id === session.id));

  const other = await agents(user).createSession(agent.agentId, {
    idempotencyKey: `session-${randomUUID()}`,
  });
  assert.equal(other.data.template.templateId, secondTemplateId);
  const page = await agents(user).listSessions(agent.agentId, { limit: 1 });
  assert.deepEqual(page.sessions.map((item) => item.id), [other.id]);
  assert.equal(page.hasMore, true);
  assert(page.sessions[0] instanceof Session);
  const rest = await agents(user).listSessions(agent.agentId, { cursor: page.nextCursor });
  assert.deepEqual(rest.sessions.map((item) => item.id), [session.id]);

  await other.delete();
  assert.deepEqual(
    (await agents(user).listSessions(agent.agentId)).sessions.map((item) => item.id),
    [session.id],
  );
  void edited;
});

test("the Agent's sessionDefaults create the same Session without the Agent", async () => {
  const agent = await createAgent("Equivalent");
  const key = `session-${randomUUID()}`;
  const through = await agents(user).createSession(agent.agentId, { idempotencyKey: key });
  const ordinary = await user.createSession({ ...agent.sessionDefaults, idempotencyKey: key });
  assert.equal(ordinary.id, through.id);
  const fresh = await user.createSession({
    ...agent.sessionDefaults,
    idempotencyKey: `session-${randomUUID()}`,
  });
  assert.notEqual(fresh.id, through.id);
  assert.deepEqual(fresh.data.template, through.data.template);
  assert.deepEqual(fresh.data.billingTag, through.data.billingTag);
});
