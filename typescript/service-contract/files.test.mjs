import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const bytes = await readFile(new URL("files.json", import.meta.url));
const contract = JSON.parse(bytes);
const manifest = JSON.parse(
  await readFile(new URL("../package.json", import.meta.url)),
);
const metadata = JSON.parse(
  await readFile(new URL("metadata.json", import.meta.url)),
);
const upload = "/api/v1/org/users/{userId}/drive/uploads";

test("file transfers retain the exact selected service contract and separate User and ticket authority", () => {
  assert.equal(
    createHash("sha256").update(bytes).digest("hex"),
    metadata.snapshots["files.json"],
  );
  assert.equal(metadata.tidbLinkRevision, manifest.tidbLinkRevision);
  assert.equal(contract.revision, manifest.tidbLinkRevision);
  assert.equal(
    contract.sourceOpenApiSha256,
    manifest.tidbLinkOpenApiSha256,
  );
  for (const [path, methods] of Object.entries(contract.paths)) {
    for (const operation of Object.values(methods)) {
      assert.deepEqual(
        operation.security,
        path.includes("{ticket}") ? [] : path.includes("/org/users/") ? [{ OrganizationApiKey: [] }] : [{ UserApiKey: [] }],
      );
    }
  }
  assert(
    contract.paths[upload].post.parameters.some(
      (header) => header.name === "Idempotency-Key" && header.required,
    ),
  );
  for (const header of ["Content-Length", "Content-Digest"])
    assert(
      contract.paths[`${upload}/{uploadId}/content`].put.parameters.some(
        (value) => value.name === header && value.required,
      ),
    );
  assert.equal(
    contract.paths[`${upload}/{uploadId}/content`].put.requestBody.content[
      "application/octet-stream"
    ].schema.format,
    "binary",
  );
  assert.deepEqual(
    contract.components.schemas.FileUploadResponseDto.properties.upload
      .properties.expectedChecksumVerification.enum,
    ["whole"],
  );
  const part =
    contract.components.schemas.PublicUserFileUploadPartResponseOpenApiDto
      .properties.part;
  assert.deepEqual(part.required, [
    "number",
    "url",
    "size",
    "headers",
    "expiresAt",
  ]);
  assert.deepEqual(
    contract.components.schemas.FileConfirmationDto.properties.parts.items
      .required,
    ["number", "etag", "sha256"],
  );
  const content =
    contract.paths["/api/v1/org/users/{userId}/files/{fileId}/content"].get.responses["200"]
      .content;
  assert.equal(content["*/*"].schema.format, "binary");
  assert.deepEqual(
    content["application/vnd.tidb-link.file-download+json"].schema.required,
    ["url", "expiresAt", "byteSize", "sha256"],
  );
  assert(
    contract.paths["/api/v1/drive-downloads/{ticket}"].get.responses["302"],
  );
  assert.deepEqual(contract.components.schemas.FilesPageDto.required, [
    "files",
    "nextCursor",
  ]);
});

const sessionFiles = "/api/v1/org/users/{userId}/sessions/{id}/files";

test("files in a Session's Drive are read by path and version, with a distinct version conflict", () => {
  const read = contract.paths[`${sessionFiles}/content`].get;
  const query = Object.fromEntries(
    read.parameters
      .filter((parameter) => parameter.in === "query")
      .map((parameter) => [parameter.name, parameter]),
  );
  assert.equal(query.path.required, true);
  assert.equal(query.version.required, false);
  // The SDK passes the version on as it was announced and leaves its form to the service.
  assert.equal(query.version.schema.type, "string");
  const content = read.responses["200"].content;
  assert.equal(content["*/*"].schema.format, "binary");
  // A large file answers with a ticket, which has no SHA-256, unlike File content.
  assert.deepEqual(
    content["application/vnd.tidb-link.file-download+json"].schema.required,
    ["url", "expiresAt", "byteSize"],
  );
  const ticket = contract.paths[`${sessionFiles}/download-tickets`].post;
  const request =
    contract.components.schemas[
      ticket.requestBody.content["application/json"].schema.$ref.split("/").at(-1)
    ];
  assert.deepEqual(request.required, ["path"]);
  assert.deepEqual(Object.keys(request.properties).sort(), ["path", "version"]);
  const answer =
    contract.components.schemas[
      ticket.responses["201"].content["application/json"].schema.$ref.split("/").at(-1)
    ];
  assert.deepEqual(answer.required, ["url", "expiresAt", "byteSize"]);
  for (const operation of [read, ticket]) {
    assert.match(operation.responses["404"].description, /session_not_found/);
    assert.match(operation.responses["404"].description, /drive_file_not_found/);
    assert.match(operation.responses["409"].description, /drive_file_version_conflict/);
  }
  assert.match(
    contract.paths["/api/v1/drive-downloads/{ticket}"].get.responses["409"]
      .description,
    /drive_file_version_conflict/,
  );
});
