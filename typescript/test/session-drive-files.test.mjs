import assert from "node:assert/strict";
import { createServer } from "node:http";
import { after, test } from "node:test";
import {
  ConflictError,
  DriveFileVersionConflictError,
  OrganizationClient,
  TiDBLinkApiError,
  TiDBLinkError,
} from "../dist/index.js";

const apiKey = "ti_org_drive_" + "x".repeat(43);
const servers = [];
after(async () =>
  Promise.all(
    servers.map((server) => new Promise((resolve) => server.close(resolve))),
  ),
);
const serve = async (handler) => {
  const server = createServer(handler);
  servers.push(server);
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  return `http://127.0.0.1:${server.address().port}`;
};
const sessionPath = "/api/v1/org/users/user_1/sessions/sess_1";
const refuse = (res, status, code) =>
  res
    .writeHead(status, {
      "content-type": "application/json",
      "x-request-id": `req-${code}`,
    })
    .end(JSON.stringify({ error: { code, message: `refused: ${code}` } }));
const sessionOf = (baseUrl) =>
  new OrganizationClient({ baseUrl, apiKey }).user("user_1").session("sess_1");

test("read returns the bytes of a file in the Session's Drive, pinned to the announced version", async () => {
  const requests = [];
  const baseUrl = await serve((req, res) => {
    requests.push({ url: req.url, authorization: req.headers.authorization });
    res
      .writeHead(200, { "content-type": "application/octet-stream" })
      .end("firmware");
  });
  const session = sessionOf(baseUrl);
  const bytes = await session.files.read("build/firmware v2.bin", {
    version: "v1_AAAA-BBBB",
  });
  assert(bytes instanceof Uint8Array);
  assert.equal(new TextDecoder().decode(bytes), "firmware");
  await session.files.read("notes.md");
  assert.deepEqual(requests, [
    {
      url: `${sessionPath}/files/content?path=build%2Ffirmware+v2.bin&version=v1_AAAA-BBBB`,
      authorization: `Bearer ${apiKey}`,
    },
    {
      url: `${sessionPath}/files/content?path=notes.md`,
      authorization: `Bearer ${apiKey}`,
    },
  ]);
});

test("a changed file raises DriveFileVersionConflictError, distinct from a missing file or Session", async () => {
  const baseUrl = await serve((req, res) => {
    const path = new URL(req.url, "http://x").searchParams.get("path");
    if (path === "changed.bin") refuse(res, 409, "drive_file_version_conflict");
    else if (path === "gone.bin") refuse(res, 404, "drive_file_not_found");
    else if (path === "stale.json") refuse(res, 409, "other_conflict");
    else refuse(res, 404, "session_not_found");
  });
  const files = sessionOf(baseUrl).files;
  await assert.rejects(
    files.read("changed.bin", { version: "v1" }),
    (error) =>
      error instanceof DriveFileVersionConflictError &&
      error instanceof ConflictError &&
      error.status === 409 &&
      error.code === "drive_file_version_conflict" &&
      error.requestId === "req-drive_file_version_conflict",
  );
  for (const [path, code] of [
    ["gone.bin", "drive_file_not_found"],
    ["deleted-session.bin", "session_not_found"],
  ])
    await assert.rejects(
      files.read(path, { version: "v1" }),
      (error) =>
        error instanceof TiDBLinkApiError &&
        !(error instanceof ConflictError) &&
        error.status === 404 &&
        error.code === code,
    );
  // Only the version conflict code is a version conflict.
  await assert.rejects(
    files.read("stale.json"),
    (error) =>
      error instanceof ConflictError &&
      !(error instanceof DriveFileVersionConflictError),
  );
});

test("a large file's read follows the download ticket the service answers with, without the API Key", async () => {
  const redemptions = [];
  const ticket = (name, byteSize = 5, expiresAt = "2099-01-01T00:00:00.000Z") => ({
    url: `/api/v1/drive-downloads/v1.session-file.k1.${name}`,
    expiresAt,
    byteSize,
  });
  const baseUrl = await serve((req, res) => {
    if (req.url.startsWith(`${sessionPath}/files/content?`)) {
      const query = new URL(req.url, "http://x").searchParams;
      const name = query.get("path").replace(".bin", "");
      res
        .writeHead(200, {
          "content-type": "application/vnd.tidb-link.file-download+json",
        })
        .end(
          JSON.stringify(
            name === "expired"
              ? ticket(name, 5, "2000-01-01T00:00:00.000Z")
              : name === "truncated"
                ? ticket(name, 6)
                : name === "unusable"
                  ? { url: ticket(name).url, byteSize: 5 }
                  : ticket(`${name}.${query.get("version") ?? "current"}`),
          ),
        );
      return;
    }
    redemptions.push({ url: req.url, authorization: req.headers.authorization });
    if (req.url.endsWith(".changed.v1"))
      refuse(res, 409, "drive_file_version_conflict");
    else if (req.url.endsWith(".gone.v1")) refuse(res, 404, "drive_file_not_found");
    else
      res
        .writeHead(200, { "content-type": "application/octet-stream" })
        .end("large");
  });
  const files = sessionOf(baseUrl).files;
  assert.equal(
    new TextDecoder().decode(await files.read("big.bin", { version: "v1" })),
    "large",
  );
  assert.deepEqual(redemptions, [
    { url: "/api/v1/drive-downloads/v1.session-file.k1.big.v1", authorization: undefined },
  ]);
  await assert.rejects(
    files.read("changed.bin", { version: "v1" }),
    (error) =>
      error instanceof DriveFileVersionConflictError &&
      error.requestId === "req-drive_file_version_conflict",
  );
  await assert.rejects(
    files.read("gone.bin", { version: "v1" }),
    (error) =>
      error instanceof TiDBLinkApiError &&
      !(error instanceof ConflictError) &&
      error.status === 404 &&
      error.code === "drive_file_not_found",
  );
  await assert.rejects(
    files.read("truncated.bin"),
    (error) => error instanceof TiDBLinkError && /size/.test(error.message),
  );
  await assert.rejects(
    files.read("unusable.bin"),
    (error) => error instanceof TiDBLinkError && /ticket/.test(error.message),
  );
  const before = redemptions.length;
  await assert.rejects(
    files.read("expired.bin"),
    (error) => error instanceof TiDBLinkApiError && error.status === 410,
  );
  assert.equal(redemptions.length, before);
});

test("createDownloadTicket returns an absolute keyless URL for the file, pinned to its version", async () => {
  const requests = [];
  const baseUrl = await serve(async (req, res) => {
    let body = "";
    for await (const chunk of req) body += chunk;
    requests.push({
      method: req.method,
      url: req.url,
      authorization: req.headers.authorization,
      body: JSON.parse(body),
    });
    const { path } = JSON.parse(body);
    if (path === "changed.bin") refuse(res, 409, "drive_file_version_conflict");
    else if (path === "gone.bin") refuse(res, 404, "drive_file_not_found");
    else
      res
        .writeHead(201, { "content-type": "application/json" })
        .end(
          JSON.stringify({
            url: "/api/v1/drive-downloads/v1.session-file.k1.opaque",
            expiresAt: "2099-01-01T00:01:00.000Z",
            byteSize: 4096,
          }),
        );
  });
  const files = sessionOf(baseUrl).files;
  assert.deepEqual(
    await files.createDownloadTicket("build/firmware.bin", { version: "v1" }),
    {
      url: `${baseUrl}/api/v1/drive-downloads/v1.session-file.k1.opaque`,
      expiresAt: "2099-01-01T00:01:00.000Z",
      byteSize: 4096,
    },
  );
  await files.createDownloadTicket("notes.md");
  assert.deepEqual(requests, [
    {
      method: "POST",
      url: `${sessionPath}/files/download-tickets`,
      authorization: `Bearer ${apiKey}`,
      body: { path: "build/firmware.bin", version: "v1" },
    },
    {
      method: "POST",
      url: `${sessionPath}/files/download-tickets`,
      authorization: `Bearer ${apiKey}`,
      body: { path: "notes.md" },
    },
  ]);
  await assert.rejects(
    files.createDownloadTicket("changed.bin", { version: "v1" }),
    DriveFileVersionConflictError,
  );
  await assert.rejects(
    files.createDownloadTicket("gone.bin", { version: "v1" }),
    (error) =>
      error instanceof TiDBLinkApiError &&
      !(error instanceof ConflictError) &&
      error.code === "drive_file_not_found",
  );
});

test("a version is opaque: the service, not the SDK, judges its form", async () => {
  const requests = [];
  const baseUrl = await serve(async (req, res) => {
    let body = "";
    for await (const chunk of req) body += chunk;
    requests.push(
      req.method === "GET"
        ? new URL(req.url, "http://x").searchParams.get("version")
        : JSON.parse(body).version,
    );
    refuse(res, 400, "validation_error");
  });
  const files = sessionOf(baseUrl).files;
  const version = `a future/version form+${"x".repeat(40)}`;
  for (const pinned of [
    files.read("a.bin", { version }),
    files.createDownloadTicket("a.bin", { version }),
  ])
    await assert.rejects(
      pinned,
      (error) =>
        error instanceof TiDBLinkApiError &&
        error.status === 400 &&
        error.code === "validation_error",
    );
  assert.deepEqual(requests, [version, version]);
});

test("invalid Session file arguments never dispatch", async () => {
  let requests = 0;
  const baseUrl = await serve((req, res) => {
    requests++;
    res.writeHead(500).end();
  });
  const files = sessionOf(baseUrl).files;
  for (const [path, options] of [
    ["", {}],
    [7, {}],
    ["a.bin", { version: "" }],
    ["a.bin", { version: 1 }],
    ["a.bin", { unknown: true }],
  ]) {
    await assert.rejects(files.read(path, options), TypeError);
    await assert.rejects(files.createDownloadTicket(path, options), TypeError);
  }
  assert.equal(requests, 0);
});
