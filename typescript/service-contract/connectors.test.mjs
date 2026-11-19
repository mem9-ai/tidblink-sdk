import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const bytes = await readFile(new URL("connectors.json", import.meta.url));
const contract = JSON.parse(bytes);
const manifest = JSON.parse(
  await readFile(new URL("../package.json", import.meta.url)),
);
const metadata = JSON.parse(
  await readFile(new URL("metadata.json", import.meta.url)),
);
const connectors = "/api/v1/org/connectors";
const success = (operation, status) =>
  operation.responses[status].content["application/json"].schema;

test("the SDK freezes only the Connector reads, under Organization authority", () => {
  assert.equal(
    createHash("sha256").update(bytes).digest("hex"),
    metadata.snapshots["connectors.json"],
  );
  assert.equal(contract.revision, manifest.tidbLinkRevision);
  assert.equal(contract.sourceOpenApiSha256, manifest.tidbLinkOpenApiSha256);
  assert.deepEqual(Object.keys(contract.paths).sort(), [connectors, `${connectors}/{connectorId}`]);
  for (const methods of Object.values(contract.paths)) {
    assert.deepEqual(Object.keys(methods).filter((key) => key !== "parameters"), ["get"]);
    assert.deepEqual(methods.get.security, [{ OrganizationApiKey: [] }]);
  }
  // Serving over the Backend Connection replaces registration and callbacks.
  assert(!JSON.stringify(contract).includes("/api/v1/connectors/jwks"));
});

test("a Connector record is the declared Connector only, without transport or registration fields", () => {
  const record = success(contract.paths[`${connectors}/{connectorId}`].get, "200");
  assert.deepEqual(record.required, ["connectorId", "displayName", "createdAt", "updatedAt"]);
  assert.deepEqual(Object.keys(record.properties).sort(), ["connectorId", "createdAt", "displayName", "updatedAt"]);
  assert.equal(record.additionalProperties, false);
  assert.match(record.description, /connector\.serve/);
  const page = success(contract.paths[connectors].get, "200");
  assert.deepEqual(page.required, ["connectors"]);
  assert.match(contract.paths[connectors].get.description, /whether or not a connection serves it now/);
});
