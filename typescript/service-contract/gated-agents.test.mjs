import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const bytes = await readFile(new URL("gated-agents.json", import.meta.url));
const contract = JSON.parse(bytes);
const ordinary = JSON.parse(
  await readFile(new URL("openapi.json", import.meta.url)),
);
const manifest = JSON.parse(
  await readFile(new URL("../package.json", import.meta.url)),
);
const metadata = JSON.parse(
  await readFile(new URL("metadata.json", import.meta.url)),
);
const agents = "/api/v1/org/users/{userId}/agents";
const agent = `${agents}/{agentId}`;
const sessions = `${agent}/sessions`;
const schemas = contract.components.schemas;
const resolve = (document, value) =>
  value.$ref
    ? resolve(document, document.components.schemas[value.$ref.split("/").at(-1)])
    : value;
const body = (operation) =>
  resolve(contract, operation.requestBody.content["application/json"].schema);
const success = (document, operation, status) =>
  resolve(document, operation.responses[status].content["application/json"].schema);
const operations = () =>
  Object.values(contract.paths).flatMap((item) =>
    Object.entries(item).filter(([method]) => method !== "parameters"),
  );

test("the SDK freezes the gated Agent routes under Organization authority", () => {
  assert.equal(
    createHash("sha256").update(bytes).digest("hex"),
    metadata.snapshots["gated-agents.json"],
  );
  assert.equal(contract.revision, manifest.tidbLinkRevision);
  assert.equal(contract.sourceOpenApiSha256, manifest.tidbLinkOpenApiSha256);
  assert.deepEqual(Object.keys(contract.paths).sort(), [agents, agent, sessions]);
  const methods = (path) =>
    Object.keys(contract.paths[path]).filter((key) => key !== "parameters").sort();
  assert.deepEqual(methods(agents), ["get", "post"]);
  assert.deepEqual(methods(agent), ["delete", "get", "patch"]);
  assert.deepEqual(methods(sessions), ["get", "post"]);
  for (const [, operation] of operations())
    assert.deepEqual(operation.security, [{ OrganizationApiKey: [] }]);
});

test("every Agent route refuses an Organization that is not enabled with the one code the SDK maps", () => {
  for (const [, operation] of operations())
    assert.match(operation.responses["403"].description, /agent_preview_not_enabled/);
  const codes = schemas.ErrorEnvelope.properties.error.properties.code.enum;
  assert(codes.includes("agent_preview_not_enabled"));
  assert(codes.includes("agent_not_found"));
});

test("Agent Session defaults are Session creation's fields without tools", () => {
  const creation = resolve(
    ordinary,
    ordinary.paths["/api/v1/org/users/{userId}/sessions"].post.requestBody
      .content["application/json"].schema,
  );
  const create = body(contract.paths[agents].post);
  assert.deepEqual(create.required, ["name", "sessionDefaults"]);
  const defaults = create.properties.sessionDefaults;
  assert.deepEqual(defaults.required, ["agentTemplateId"]);
  assert.deepEqual(
    Object.keys(defaults.properties).sort(),
    Object.keys(creation.properties).filter((key) => key !== "tools").sort(),
  );
  const update = body(contract.paths[agent].patch);
  assert.equal(update.required, undefined);
  assert.deepEqual(Object.keys(update.properties).sort(), ["name", "sessionDefaults"]);
  assert.deepEqual(
    Object.keys(update.properties.sessionDefaults.properties).sort(),
    Object.keys(defaults.properties).sort(),
  );
  const view = success(contract, contract.paths[agent].get, "200").properties.agent;
  assert.deepEqual(view.required, [
    "agentId",
    "userId",
    "name",
    "sessionDefaults",
    "createdAt",
    "updatedAt",
  ]);
  assert.deepEqual(
    Object.keys(view.properties.sessionDefaults.properties).sort(),
    Object.keys(defaults.properties).sort(),
  );
});

test("creation is keyed, an update needs If-Match and only single reads carry an ETag", () => {
  const header = (operation, name) =>
    operation.parameters.find(
      (parameter) => parameter.in === "header" && parameter.name === name,
    );
  for (const operation of [contract.paths[agents].post, contract.paths[sessions].post])
    assert.equal(header(operation, "Idempotency-Key").required, true);
  assert.equal(header(contract.paths[agent].patch, "If-Match").required, true);
  assert(contract.paths[agent].patch.responses["412"]);
  for (const [operation, status] of [
    [contract.paths[agents].post, "201"],
    [contract.paths[agent].get, "200"],
    [contract.paths[agent].patch, "200"],
    [contract.paths[sessions].post, "201"],
  ])
    assert(operation.responses[status].headers.ETag, status);
  assert.equal(contract.paths[agents].get.responses["200"].headers, undefined);
  const page = success(contract, contract.paths[agents].get, "200");
  assert.deepEqual(Object.keys(page.properties).sort(), ["agents", "hasMore", "nextCursor"]);
  assert(contract.paths[agent].delete.responses["204"]);
});

test("Session creation through an Agent takes only per-Session choices and returns an ordinary Session", () => {
  const choices = body(contract.paths[sessions].post);
  assert.deepEqual(Object.keys(choices.properties).sort(), [
    "billingTag",
    "model",
    "realtimeModel",
    "realtimeVoice",
    "tools",
  ]);
  // A null voice override drops the Agent's voice.
  assert.equal(choices.properties.realtimeVoice.nullable, true);
  const created = success(contract, contract.paths[sessions].post, "201");
  assert.deepEqual(Object.keys(created.properties).sort(), ["agentId", "session"]);
  const session = success(
    ordinary,
    ordinary.paths["/api/v1/org/users/{userId}/sessions/{id}"].get,
    "200",
  ).properties.session;
  assert.deepEqual(
    Object.keys(created.properties.session.properties).sort(),
    Object.keys(resolve(ordinary, session).properties).sort(),
  );
  const listed = success(contract, contract.paths[sessions].get, "200");
  const ordinaryList = success(
    ordinary,
    ordinary.paths["/api/v1/org/users/{userId}/sessions"].get,
    "200",
  );
  assert.deepEqual(
    Object.keys(listed.properties.sessions.items.properties).sort(),
    Object.keys(resolve(ordinary, ordinaryList.properties.sessions.items).properties).sort(),
  );
});
