import assert from "node:assert/strict";
import { randomUUID, createHash } from "node:crypto";
import { test } from "node:test";
import {
  ConflictError,
  DriveFileVersionConflictError,
  OrganizationClient,
  TiDBLinkApiError,
} from "@tidblink/server";
import { uploadFileWorkflow } from "@tidblink/server/examples/upload-file.mjs";

const baseUrl = process.env.SDK_SERVICE_URL;
const local = new OrganizationClient({
  baseUrl,
  apiKey: process.env.SDK_ORGANIZATION_KEY,
}).user(process.env.SDK_USER_ID);
const remote = new OrganizationClient({
  baseUrl,
  apiKey: process.env.SDK_ORGANIZATION_KEY,
}).user(process.env.SDK_FILE_REMOTE_USER_ID);
const stranger = new OrganizationClient({
  baseUrl,
  apiKey: process.env.SDK_ORGANIZATION_KEY,
}).user(process.env.SDK_FOREIGN_USER_ID);
const sha = (bytes) => createHash("sha256").update(bytes).digest("hex");
const metadata = (bytes, originalName = "note.txt") => ({
  originalName,
  byteSize: bytes.length,
  sha256: sha(bytes),
  contentType: "text/plain",
  idempotencyKey: randomUUID(),
});
const newSession = async () =>
  local.createSession({
    templateId: process.env.SDK_TEMPLATE_ID,
    idempotencyKey: randomUUID(),
  });

test("actual inline/empty uploads become verified files and selected bytes participate in a real Turn", async () => {
  const secret = `only-in-file-${randomUUID()}`;
  const bytes = Buffer.from(secret);
  const input = {
    originalName: "note.txt",
    bytes,
    contentType: "text/plain",
    idempotencyKey: randomUUID(),
  };
  const file = await local.files.upload(input);
  assert.equal(file.status, "ready");
  assert.equal(file.sha256, sha(bytes));
  assert.equal((await local.files.upload(input)).fileId, file.fileId);
  assert.equal((await local.files.getUpload(file.fileId)).state, "completed");
  assert.deepEqual(Buffer.from(await local.files.download(file.fileId)), bytes);
  const empty = await local.files.upload({
    originalName: "empty.txt",
    bytes: new Uint8Array(),
    idempotencyKey: randomUUID(),
  });
  assert.equal(empty.byteSize, 0);
  assert.equal((await local.files.download(empty.fileId)).byteLength, 0);
  const page = await local.files.list({ limit: 1 });
  assert.equal(page.files.length, 1);
  assert(page.nextCursor);
  assert.equal(
    (await local.files.list({ limit: 1, cursor: page.nextCursor })).files[0]
      .fileId,
    file.fileId,
  );
  const session = await newSession();
  const result = await session.turn(
    {
      clientEventId: randomUUID(),
      text: `Read the attached file /home/user/uploads/${file.fileId}/note.txt and report its content.`,
      userFileIds: [file.fileId],
    },
    { timeoutMs: 20000, pollIntervalMs: 25 },
  );
  assert(result.text.includes(secret), result.text);
  const turn = await session.getTurn(result.turnId);
  assert.equal(turn.userFiles[0].userFileId, file.fileId);
  await assert.rejects(
    stranger.files.get(file.fileId),
    (error) => error instanceof TiDBLinkApiError && error.status === 404,
  );
  const strangerSession = await stranger.createSession({
    templateId: process.env.SDK_TEMPLATE_ID,
    idempotencyKey: randomUUID(),
  });
  await assert.rejects(
    strangerSession.submitMessage({
      clientEventId: randomUUID(),
      text: "Read foreign file",
      userFileIds: [file.fileId],
    }),
    (error) =>
      error instanceof TiDBLinkApiError && [400, 404].includes(error.status),
  );
});

test("actual multipart bytes are presigned, transferred, whole-file verified and downloaded", async () => {
  const capabilities = await remote.files.capabilities();
  assert.equal(capabilities.inlineThreshold, 5);
  const bytes = Buffer.from("multipart exact bytes and checksum");
  const file = await remote.files.upload({
    originalName: "multipart.txt",
    bytes,
    contentType: "text/plain",
    idempotencyKey: randomUUID(),
  });
  assert.equal(file.status, "ready");
  assert.equal(file.byteSize, bytes.length);
  assert.equal(file.sha256, sha(bytes));
  assert.equal(file.checksumVerification, "whole");
  assert.deepEqual(
    Buffer.from(await remote.files.download(file.fileId)),
    bytes,
  );
  assert.equal((await remote.files.getUpload(file.fileId)).mode, "s3");
});

test("actual uploads retain identity and refuse incomplete, cancelled and corrupt content", async () => {
  const bytes = Buffer.from("abc");
  const input = metadata(bytes);
  const upload = await local.files.createUpload(input);
  const replay = await local.files.createUpload(input);
  assert.equal(replay.uploadId, upload.uploadId);
  assert.equal(replay.expiresAt, upload.expiresAt);
  await assert.rejects(
    local.files.createUpload({ ...input, originalName: "changed.txt" }),
    (error) => error instanceof TiDBLinkApiError && error.status === 409,
  );
  await assert.rejects(
    local.files.completeUpload(upload.uploadId),
    (error) =>
      error instanceof TiDBLinkApiError && error.code === "upload_incomplete",
  );
  const session = await newSession();
  await assert.rejects(
    session.submitMessage({
      clientEventId: randomUUID(),
      text: "Read unfinished file",
      userFileIds: [upload.uploadId],
    }),
    (error) => error instanceof TiDBLinkApiError,
  );
  const cancelled = await local.files.cancelUpload(upload.uploadId);
  assert.equal(cancelled.status, "failed");
  assert.equal((await local.files.getUpload(upload.uploadId)).state, "aborted");
  await assert.rejects(
    local.files.createUpload(input),
    (error) => error instanceof TiDBLinkApiError && error.status === 409,
  );
  const corrupt = await local.files.createUpload(metadata(bytes));
  await assert.rejects(
    local.files.uploadContent(corrupt.uploadId, Buffer.from("wrong")),
    (error) =>
      error instanceof TiDBLinkApiError && [400, 422].includes(error.status),
  );
  assert.notEqual(
    (await local.files.getUpload(corrupt.uploadId)).file.status,
    "ready",
  );
});

test("actual large-file content returns a bearer download ticket that the SDK redeems without a key", async () => {
  const bytes = Buffer.alloc(50 * 1024 * 1024 + 1, 97);
  const file = await local.files.upload({
    originalName: "large.bin",
    bytes,
    idempotencyKey: randomUUID(),
  });
  const content = await local.files.getContent(file.fileId);
  assert.equal(content.type, "download");
  assert.equal(content.grant.byteSize, bytes.length);
  assert.deepEqual(
    Buffer.from(await local.files.downloadFromGrant(content.grant)),
    bytes,
  );
});

test("actual provider content corruption cannot become a ready file", async () => {
  await assert.rejects(
    remote.files.upload({
      originalName: "corrupt.txt",
      bytes: Buffer.from("corruption must be detected"),
      idempotencyKey: randomUUID(),
    }),
    (error) =>
      error instanceof TiDBLinkApiError &&
      error.code === "upload_integrity_failed",
  );
});

test("the installed upload example includes file content in its conversation", async () => {
  const session = await newSession();
  const unique = `example-file-${randomUUID()}`;
  const result = await uploadFileWorkflow({
    baseUrl,
    organizationApiKey: process.env.SDK_ORGANIZATION_KEY,
    userId: process.env.SDK_USER_ID,
    sessionId: session.id,
    originalName: "example.txt",
    bytes: Buffer.from(unique),
    idempotencyKey: randomUUID(),
    clientEventId: randomUUID(),
    timeoutMs: 20000,
  });
  assert.equal(result.file.status, "ready");
  assert(result.result.text.includes(unique), result.result.text);
});

test("a File Notification reaches onFile and its Client Tool reads the announced version, which a rewrite retires", async () => {
  const organization = new OrganizationClient({
    baseUrl,
    apiKey: process.env.SDK_ORGANIZATION_KEY,
  });
  try {
    const user = organization.user(process.env.SDK_USER_ID);
    const session = await user.createSession({
      templateId: process.env.SDK_TEMPLATE_ID,
      idempotencyKey: randomUUID(),
    });
    const announced = [];
    const flashed = [];
    const conn = await session.connect({
      tools: {
        flash_device: {
          description:
            "Flash a firmware image onto the device. Call notify_file first and pass its path and version.",
          parameters: {
            type: "object",
            properties: { path: { type: "string" }, version: { type: "string" } },
            required: ["path", "version"],
            additionalProperties: false,
          },
          run: async ({ path, version }) => {
            const bytes = await session.files.read(path, { version });
            const ticket = await session.files.createDownloadTicket(path, { version });
            // The device downloads the ticket's URL without a key.
            const download = await fetch(ticket.url);
            flashed.push({
              bytes: Buffer.from(bytes).toString(),
              ticket,
              download: { status: download.status, text: await download.text() },
            });
            return "flashed-ok";
          },
        },
      },
      onFile: (file) => announced.push(file),
    });
    const reply = await conn.send("SDK_FIXTURE_NOTIFY_FILE: build the firmware and flash it", {
      timeoutMs: 30000,
    });
    assert.equal(reply.text, "Device answered: flashed-ok");
    assert.equal(announced.length, 1);
    const [file] = announced;
    assert.deepEqual(
      { ...file, version: undefined, updatedAt: undefined },
      {
        path: "builds/firmware.bin",
        name: "Firmware 1",
        version: undefined,
        byteSize: "firmware v1".length,
        updatedAt: undefined,
      },
    );
    assert.equal(typeof file.version, "string");
    assert(file.version);
    assert.equal(flashed.length, 1);
    assert.equal(flashed[0].bytes, "firmware v1");
    assert.equal(flashed[0].ticket.byteSize, file.byteSize);
    assert(flashed[0].ticket.url.startsWith(`${baseUrl}/api/v1/drive-downloads/`));
    assert.deepEqual(flashed[0].download, { status: 200, text: "firmware v1" });
    // A late reader recovers the announcement from the Turn's Operations.
    const turn = await session.getTurn(reply.turnId);
    assert.deepEqual(
      turn.operations
        .filter((operation) => operation.kind === "file.notify")
        .map((operation) => operation.resultPointer),
      [{ kind: "file_notification", ...file }],
    );
    await conn.close();

    // Rewriting the file retires the announced version.
    const rewrite = await fetch(
      `${baseUrl}/api/v1/org/users/${process.env.SDK_USER_ID}/sessions/${session.id}/files`,
      {
        method: "POST",
        headers: {
          authorization: `Bearer ${process.env.SDK_ORGANIZATION_KEY}`,
          "content-type": "application/json",
        },
        body: JSON.stringify({ path: file.path, content: "firmware v2" }),
      },
    );
    assert.equal(rewrite.status, 201);
    for (const pinned of [
      session.files.read(file.path, { version: file.version }),
      session.files.createDownloadTicket(file.path, { version: file.version }),
    ])
      await assert.rejects(
        pinned,
        (error) =>
          error instanceof DriveFileVersionConflictError &&
          error.code === "drive_file_version_conflict",
      );
    const stale = await fetch(flashed[0].ticket.url);
    assert.equal(stale.status, 409);
    assert.equal(
      Buffer.from(await session.files.read(file.path)).toString(),
      "firmware v2",
    );
    await assert.rejects(
      session.files.read("builds/missing.bin"),
      (error) =>
        error instanceof TiDBLinkApiError &&
        !(error instanceof ConflictError) &&
        error.status === 404 &&
        error.code === "drive_file_not_found",
    );
    // Another End User's handle cannot read this Session's Drive.
    await assert.rejects(
      organization
        .user(process.env.SDK_FOREIGN_USER_ID)
        .session(session.id)
        .files.read(file.path),
      (error) =>
        error instanceof TiDBLinkApiError &&
        error.status === 404 &&
        error.code === "session_not_found",
    );
  } finally {
    await organization.close();
  }
});
