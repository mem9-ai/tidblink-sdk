import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const bytes = await readFile(new URL("audio.json", import.meta.url));
const contract = JSON.parse(bytes);
const manifest = JSON.parse(
  await readFile(new URL("../package.json", import.meta.url)),
);
const metadata = JSON.parse(
  await readFile(new URL("metadata.json", import.meta.url)),
);

test("prerecorded audio keeps its multipart request and distinct response-owned event contract", () => {
  assert.equal(
    createHash("sha256").update(bytes).digest("hex"),
    metadata.snapshots["audio.json"],
  );
  assert.equal(metadata.tidbLinkRevision, manifest.tidbLinkRevision);
  assert.equal(contract.revision, manifest.tidbLinkRevision);
  assert.equal(
    contract.sourceOpenApiSha256,
    manifest.tidbLinkOpenApiSha256,
  );
  const operation =
    contract.paths["/api/v1/org/users/{userId}/sessions/{id}/turns/audio"].post;
  assert.deepEqual(operation.security, [{ OrganizationApiKey: [] }]);
  const multipart = operation.requestBody.content["multipart/form-data"];
  assert.deepEqual(multipart.schema.required, ["file"]);
  assert.deepEqual(Object.keys(multipart.schema.properties).sort(), [
    "billingTag",
    "file",
  ]);
  assert.equal(multipart.schema.properties.file.format, "binary");
  assert.equal(multipart.encoding.billingTag.contentType, "application/json");
  assert.equal(multipart.schema.properties.billingTag.nullable, true);
  const success = operation.responses["201"];
  // Every call carries claimBy, and none is framed without an executing connection.
  assert.match(
    success.description,
    /Its client_tool_calls frames carry claimBy: only the Session’s executing Backend Connection attachment executes and answers them; a call published while the Session has none is not performed and gets no frame/,
  );
  // The Organization route names TurnStreamEvent only in prose; it omits the
  // x-sse-event-schema link that the retired User route carried.
  assert.match(
    success.content["text/event-stream"].schema.description,
    /TurnStreamEvent/,
  );
  assert.deepEqual(success.headers["Cache-Control"].schema.enum, [
    "no-cache, no-transform",
  ]);
  for (const status of ["409", "501"])
    assert(operation.responses[status]);
  const events = contract.components.schemas.TurnStreamEvent.oneOf;
  assert.deepEqual(
    events.map((event) => event.properties.event.enum[0]).sort(),
    [
      "assistant_draft",
      "assistant_message",
      "client_tool_calls",
      "operation_step",
      "progress",
      "turn_error",
      "turn_finished",
      "turn_started",
    ],
  );
  const calls = events.find(
    (event) => event.properties.event.enum[0] === "client_tool_calls",
  ).properties.payload.properties.calls.items;
  assert.deepEqual(calls.required, ["callId", "name", "arguments", "deadlineAt"]);
  assert.equal(calls.properties.claimBy.type, "string");
  for (const event of events)
    assert.deepEqual(event.required, [
      "event",
      "turnId",
      "sessionId",
      "seq",
      "createdAt",
      "payload",
    ]);
  const started = events.find(
    (event) => event.properties.event.enum[0] === "turn_started",
  );
  assert(!(started.properties.payload.required ?? []).includes("inputId"));
  const terminal = events.find(
    (event) => event.properties.event.enum[0] === "turn_finished",
  );
  assert.deepEqual(terminal.properties.payload.properties.status.enum, [
    "succeeded",
    "failed",
    "interrupted",
  ]);
  // A File Notification's step carries the announced file, as onFile receives it.
  const step = events.find(
    (event) => event.properties.event.enum[0] === "operation_step",
  ).properties.payload;
  assert(!step.required.includes("file"));
  const file = step.properties.file;
  assert.deepEqual(file.required, ["path", "name", "version", "byteSize", "updatedAt"]);
  assert.equal(file.properties.updatedAt.nullable, true);
  assert.equal(file.properties.version.type, "string");
  // Its failed step says why it announced nothing, as FileNotificationRefusal types it.
  assert(!step.required.includes("fileRefusal"));
  const refusal = step.properties.fileRefusal;
  assert.deepEqual(refusal.required, ["reason"]);
  assert.deepEqual(Object.keys(refusal.properties).sort(), ["path", "reason"]);
  assert.equal(refusal.properties.reason.type, "string");
  assert(refusal.properties.reason.enum.includes("local_only"));
  assert.equal(refusal.properties.path.type, "string");
});


test("Organization audio binds the explicit User and retains the streaming contract", () => {
  const operation = contract.paths["/api/v1/org/users/{userId}/sessions/{id}/turns/audio"].post;
  assert.deepEqual(operation.security, [{ OrganizationApiKey: [] }]);
  assert(operation.parameters.some((p) => p.name === "userId" && p.required));
  assert(operation.responses["201"].content["text/event-stream"]);
});
