import assert from "node:assert/strict";
import { createServer } from "node:http";
import { after, test } from "node:test";
import {
  ConnectionError,
  FileUploadError,
  FileUploadOutcomeUnknownError,
  OrganizationClient,
  OutcomeUnknownError,
  TiDBLinkApiError,
} from "../dist/index.js";

const apiKey = "ti_org_file_" + "x".repeat(43);
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

test("file content uses the owned binary endpoint and preserves service refusal", async () => {
  const baseUrl = await serve((request, response) => {
    assert.equal(request.headers.authorization, `Bearer ${apiKey}`);
    if (request.url === "/api/v1/org/users/user_1/files/owned/content") {
      response
        .writeHead(200, { "content-type": "text/plain" })
        .end("owned bytes");
    } else {
      response
        .writeHead(404, {
          "content-type": "application/json",
          "x-request-id": "file-refusal",
        })
        .end(
          JSON.stringify({
            error: { code: "drive_file_not_found", message: "Not found" },
          }),
        );
    }
  });
  const user = new OrganizationClient({ baseUrl, apiKey }).user("user_1");
  const content = await user.files.getContent("owned");
  assert.equal(content.type, "bytes");
  assert.equal(new TextDecoder().decode(content.bytes), "owned bytes");
  assert.equal(content.contentType, "text/plain");
  await assert.rejects(
    user.files.getContent("foreign"),
    (error) =>
      error instanceof TiDBLinkApiError &&
      error.status === 404 &&
      error.requestId === "file-refusal",
  );
});

const ready = (fileId, bytes = "abc") => ({
  fileId,
  originalName: "note.txt",
  status: "ready",
  byteSize: Buffer.byteLength(bytes),
  sha256:
    bytes === ""
      ? "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
      : "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
  checksumVerification: "whole",
  contentType: "text/plain",
  lastError: null,
  source: { kind: "user_upload" },
  createdAt: "2026-10-28T00:00:00.000Z",
  updatedAt: "2026-10-28T00:00:00.000Z",
});
const json = (res, value, status = 200) =>
  res
    .writeHead(status, { "content-type": "application/json" })
    .end(JSON.stringify(value));
const readBytes = async (request) =>
  Buffer.concat(await Array.fromAsync(request));

test("inline upload sends exact bytes with a complete digest, confirms ready and recovers the same identity", async () => {
  const requests = [];
  const file = ready("file-inline");
  const baseUrl = await serve(async (request, response) => {
    const bytes = await readBytes(request);
    requests.push({ path: request.url, headers: request.headers, bytes });
    if (request.url.endsWith("/uploads")) {
      assert.deepEqual(JSON.parse(bytes), {
        originalName: "note.txt",
        byteSize: 3,
        sha256: file.sha256,
        contentType: "text/plain",
      });
      assert.equal(request.headers["idempotency-key"], "stable-file");
      json(
        response,
        {
          upload: {
            uploadId: file.fileId,
            mode: "tidb-link",
            expiresAt: "2099-01-01T00:00:00.000Z",
            expectedChecksumVerification: "whole",
            file: { ...file, status: "uploading" },
          },
        },
        201,
      );
    } else if (request.url.endsWith("/content")) {
      assert.equal(bytes.toString(), "abc");
      assert.equal(request.headers["content-length"], "3");
      assert.equal(
        request.headers["content-digest"],
        "sha-256=:ungWv48Bz+pBQUDeXa4iI7ADYaOWF3qctBD/YfIAFa0=:",
      );
      json(response, { file });
    } else if (request.url.endsWith("/complete")) {
      assert.deepEqual(JSON.parse(bytes), {});
      json(response, { file });
    } else {
      json(response, {
        upload: {
          uploadId: file.fileId,
          mode: "tidb-link",
          state: "completed",
          expiresAt: "2099-01-01T00:00:00.000Z",
          file,
        },
      });
    }
  });
  const user = new OrganizationClient({ baseUrl, apiKey }).user("user_1");
  assert.deepEqual(
    await user.files.upload({
      originalName: "note.txt",
      bytes: new TextEncoder().encode("abc"),
      contentType: "text/plain",
      idempotencyKey: "stable-file",
    }),
    file,
  );
  assert.equal((await user.files.getUpload(file.fileId)).state, "completed");
  assert.equal(requests.filter((r) => r.path.endsWith("/uploads")).length, 1);
});

test("multipart upload follows the service plan, binds each checksum and never sends the API Key to storage", async () => {
  const whole = new TextEncoder().encode("abc");
  const stored = [];
  const storage = await serve(async (req, res) => {
    assert.equal(req.headers.authorization, undefined);
    assert.equal(req.headers["x-request-id"], undefined);
    assert(req.headers["x-amz-checksum-sha256"]);
    stored.push(await readBytes(req));
    res.writeHead(200, { etag: `"part-${stored.length}"` }).end();
  });
  const parts = [];
  const baseUrl = await serve(async (req, res) => {
    const body = JSON.parse((await readBytes(req)).toString() || "{}");
    if (req.url.endsWith("/uploads")) {
      json(res, {
        upload: {
          uploadId: "multipart",
          mode: "s3",
          partSize: 2,
          totalParts: 2,
          expectedChecksumVerification: "whole",
          expiresAt: "2099-01-01T00:00:00.000Z",
          file: { ...ready("multipart"), status: "uploading" },
        },
      });
    } else if (req.url.endsWith("/presign")) {
      const number = Number(req.url.split("/").at(-2));
      parts.push({ number, sha256: body.sha256 });
      json(res, {
        part: {
          number,
          size: number === 1 ? 2 : 1,
          url: storage + "/part",
          headers: {
            "x-amz-checksum-sha256": Buffer.from(body.sha256, "hex").toString(
              "base64",
            ),
          },
          expiresAt: "2099-01-01T00:00:00.000Z",
        },
      });
    } else {
      assert.deepEqual(
        body.parts,
        parts.map((p) => ({ ...p, etag: `"part-${p.number}"` })),
      );
      json(res, { file: ready("multipart") });
    }
  });
  const user = new OrganizationClient({ baseUrl, apiKey }).user("user_1");
  assert.equal(
    (
      await user.files.upload({
        originalName: "note.txt",
        bytes: whole,
        idempotencyKey: "multipart-key",
      })
    ).status,
    "ready",
  );
  assert.equal(Buffer.concat(stored).toString(), "abc");
  assert.equal(
    parts[0].sha256,
    "fb8e20fc2e4c3f248c60c39bd652f3c1347298bb977b8b4d5903b85055620603",
  );
  assert.equal(
    parts[1].sha256,
    "2e7d2c03a9507ae265ecf5b5356885a53393a2029d241394997265a1a25aefc6",
  );
});

test("empty inline uploads use the supplied short-lived URL without an API Key", async () => {
  const bytes = new Uint8Array();
  const file = ready("empty", "");
  const storage = await serve(async (req, res) => {
    assert.equal(req.headers.authorization, undefined);
    assert.equal(req.headers["content-length"], "0");
    assert.equal((await readBytes(req)).length, 0);
    assert.equal(
      req.headers["content-digest"],
      "sha-256=:47DEQpj8HBSa+/TImW+5JCeuQeRkm5NMpJWZG3hSuFU=:",
    );
    json(res, { file });
  });
  const baseUrl = await serve(async (req, res) => {
    await readBytes(req);
    if (req.url.endsWith("/uploads"))
      json(res, {
        upload: {
          uploadId: file.fileId,
          mode: "tidb-link",
          expiresAt: "2099-01-01T00:00:00.000Z",
          expectedChecksumVerification: "whole",
          file: { ...file, status: "uploading" },
          uploadUrl: {
            method: "PUT",
            url: storage + "/upload?secret=ticket",
            headers: {
              "content-type": "application/octet-stream",
              "content-digest":
                "sha-256=:47DEQpj8HBSa+/TImW+5JCeuQeRkm5NMpJWZG3hSuFU=:",
            },
            expiresAt: "2099-01-01T00:00:00.000Z",
          },
        },
      });
    else json(res, { file });
  });
  assert.deepEqual(
    await new OrganizationClient({ baseUrl, apiKey }).user("user_1").files.upload({
      originalName: "empty",
      bytes,
      idempotencyKey: "empty-key",
    }),
    file,
  );
});

test("file listing preserves its own cursor/history semantics and verified downloads use ticket authority only", async () => {
  const file = ready("download");
  const storage = await serve((req, res) => {
    assert.equal(req.headers.authorization, undefined);
    res
      .writeHead(200, { "content-type": "text/plain", "content-length": "3" })
      .end("abc");
  });
  const grant = {
    url: storage + "/download?ticket=secret",
    expiresAt: "2099-01-01T00:00:00.000Z",
    byteSize: 3,
    sha256: file.sha256,
  };
  const baseUrl = await serve((req, res) => {
    if (req.url.startsWith("/api/v1/org/users/user_1/files?")) {
      const query = new URL(req.url, "http://example").searchParams;
      assert.equal(query.get("history"), "true");
      assert.equal(query.get("limit"), "1");
      assert.equal(query.get("cursor"), "cursor/+");
      json(res, { files: [file], nextCursor: "next" });
    } else if (req.url.endsWith("/content"))
      res
        .writeHead(200, {
          "content-type": "application/vnd.tidb-link.file-download+json",
        })
        .end(JSON.stringify(grant));
    else json(res, { file });
  });
  const user = new OrganizationClient({ baseUrl, apiKey }).user("user_1");
  assert.deepEqual(
    await user.files.list({ history: true, limit: 1, cursor: "cursor/+" }),
    { files: [file], nextCursor: "next" },
  );
  assert.deepEqual(await user.files.getContent("download"), {
    type: "download",
    grant,
  });
  assert.equal(
    new TextDecoder().decode(await user.files.download("download")),
    "abc",
  );
});

test("uncertain file creation retains its dispatched key after caller mutation and never repeats the effect", async () => {
  let attempts = 0;
  let sentKey;
  const input = {
    originalName: "x",
    bytes: new Uint8Array(),
    idempotencyKey: "recover-original",
  };
  const baseUrl = await serve((req) => {
    attempts++;
    sentKey = req.headers["idempotency-key"];
    input.idempotencyKey = "changed-after-dispatch";
    req.socket.destroy();
  });
  const user = new OrganizationClient({ baseUrl, apiKey }).user("user_1");
  await assert.rejects(
    user.files.upload(input),
    (error) =>
      error.name === "FileUploadOutcomeUnknownError" &&
      error.idempotencyKey === "recover-original" &&
      error.idempotencyKey === sentKey &&
      error.uploadId === undefined,
  );
  assert.equal(sentKey, "recover-original");
  assert.equal(attempts, 1);
});

test("uncertain multipart transfers retain upload identity and do not complete or replay effects", async () => {
  let attempts = 0;
  let complete = false;
  const storage = await serve(async (req, res) => {
    await readBytes(req);
    attempts++;
    res.writeHead(503).end();
  });
  const baseUrl = await serve(async (req, res) => {
    const body = JSON.parse((await readBytes(req)).toString() || "{}");
    if (req.url.endsWith("/uploads"))
      json(res, {
        upload: {
          uploadId: "uncertain-file",
          mode: "s3",
          partSize: 3,
          totalParts: 1,
          expiresAt: "2099-01-01T00:00:00.000Z",
          expectedChecksumVerification: "whole",
          file: { ...ready("uncertain-file"), status: "uploading" },
        },
      });
    else if (req.url.endsWith("/presign"))
      json(res, {
        part: {
          number: 1,
          size: 3,
          url: storage,
          headers: {
            "x-amz-checksum-sha256": Buffer.from(body.sha256, "hex").toString(
              "base64",
            ),
          },
          expiresAt: "2099-01-01T00:00:00.000Z",
        },
      });
    else {
      complete = true;
      json(res, { file: ready("uncertain-file") });
    }
  });
  await assert.rejects(
    new OrganizationClient({ baseUrl, apiKey }).user("user_1").files.upload({
      originalName: "x",
      bytes: Buffer.from("abc"),
      idempotencyKey: "retain-me",
    }),
    (error) =>
      error.name === "FileUploadOutcomeUnknownError" &&
      error.uploadId === "uncertain-file" &&
      error.idempotencyKey === "retain-me",
  );
  assert.equal(attempts, 1);
  assert.equal(complete, false);
});

test("expired and mismatched part grants are refused before any bytes are sent", async () => {
  let transfers = 0;
  const url = await serve((req, res) => {
    transfers++;
    res.end();
  });
  const files = new OrganizationClient({ baseUrl: url, apiKey }).user("user_1").files;
  const part = {
    number: 1,
    size: 3,
    url,
    headers: {
      "x-amz-checksum-sha256": "ungWv48Bz+pBQUDeXa4iI7ADYaOWF3qctBD/YfIAFa0=",
    },
    expiresAt: "2000-01-01T00:00:00.000Z",
  };
  await assert.rejects(
    files.uploadPart(part, Buffer.from("abc")),
    (error) => error.code === "file_transfer_expired",
  );
  await assert.rejects(
    files.uploadPart(
      { ...part, expiresAt: "2099-01-01T00:00:00.000Z" },
      Buffer.from("bad"),
    ),
    /checksum/,
  );
  await assert.rejects(files.uploadPart(part, Buffer.from("abcd")), /size/);
  assert.equal(transfers, 0);
});

test("upload and authenticated content redirects never send keys to another destination", async () => {
  let leaked = 0;
  const other = await serve((req, res) => {
    leaked++;
    res.end();
  });
  const baseUrl = await serve((req, res) =>
    res.writeHead(307, { location: other + "/redirected?ticket=hidden" }).end(),
  );
  const files = new OrganizationClient({ baseUrl, apiKey }).user("user_1").files;
  await assert.rejects(files.getContent("file"));
  const part = {
    number: 1,
    size: 3,
    url: baseUrl + "/signed?secret=not-for-errors",
    headers: {
      "x-amz-checksum-sha256": "ungWv48Bz+pBQUDeXa4iI7ADYaOWF3qctBD/YfIAFa0=",
    },
    expiresAt: "2099-01-01T00:00:00.000Z",
  };
  await assert.rejects(
    files.uploadPart(part, Buffer.from("abc")),
    (error) =>
      !String(error).includes("secret=") && !String(error).includes(apiKey),
  );
  assert.equal(leaked, 0);
});

test("download checks complete content and keeps expired grants unusable", async () => {
  let requests = 0;
  const baseUrl = await serve((req, res) => {
    requests++;
    res.end("abd");
  });
  const files = new OrganizationClient({ baseUrl, apiKey }).user("user_1").files;
  const grant = {
    url: baseUrl,
    expiresAt: "2099-01-01T00:00:00.000Z",
    byteSize: 3,
    sha256: ready("a").sha256,
  };
  await assert.rejects(files.downloadFromGrant(grant), /checksum/);
  await assert.rejects(
    files.downloadFromGrant({
      ...grant,
      expiresAt: "2000-01-01T00:00:00.000Z",
    }),
    (error) => error.code === "file_transfer_expired",
  );
  assert.equal(requests, 1);
});

// A fake service and storage, as a fetch, for the three upload shapes.
const uploadService = (shape) => {
  const sent = [];
  const fetch = async (input, init = {}) => {
    const url = String(input);
    const request = { url, init };
    if (init.body instanceof ReadableStream)
      request.chunks = await Array.fromAsync(init.body);
    sent.push(request);
    if (url.endsWith("/uploads"))
      return Response.json({
        upload: {
          uploadId: "shaped",
          mode: shape === "multipart" ? "s3" : "tidb-link",
          expiresAt: "2099-01-01T00:00:00.000Z",
          expectedChecksumVerification: "whole",
          file: { ...ready("shaped"), status: "uploading" },
          ...(shape === "grant"
            ? {
                uploadUrl: {
                  method: "PUT",
                  url: "https://storage.example/inline?ticket=t",
                  headers: {
                    "content-digest":
                      "sha-256=:ungWv48Bz+pBQUDeXa4iI7ADYaOWF3qctBD/YfIAFa0=:",
                  },
                  expiresAt: "2099-01-01T00:00:00.000Z",
                },
              }
            : {}),
          ...(shape === "multipart" ? { partSize: 2, totalParts: 2 } : {}),
        },
      });
    if (url.endsWith("/presign")) {
      const number = Number(url.split("/").at(-2));
      const { sha256 } = JSON.parse(init.body);
      return Response.json({
        part: {
          number,
          size: number === 1 ? 2 : 1,
          url: `https://storage.example/part-${number}`,
          headers: {
            "x-amz-checksum-sha256": Buffer.from(sha256, "hex").toString(
              "base64",
            ),
          },
          expiresAt: "2099-01-01T00:00:00.000Z",
        },
      });
    }
    if (url.startsWith("https://storage.example/"))
      return new Response(null, { headers: { etag: '"p"' } });
    return Response.json({ file: ready("shaped") });
  };
  // The requests that carried file bytes, with the bytes each should carry.
  const byteRequests = (bytes) =>
    sent
      .filter(
        (r) =>
          r.url.endsWith("/content") ||
          r.url.startsWith("https://storage.example/"),
      )
      .map((r, index) => ({
        ...r,
        expected:
          shape === "multipart"
            ? bytes.subarray(index * 2, index * 2 + 2)
            : bytes,
      }));
  return { fetch, sent, byteRequests };
};
const sameView = (actual, expected) =>
  actual instanceof Uint8Array &&
  actual.buffer === expected.buffer &&
  actual.byteOffset === expected.byteOffset &&
  actual.byteLength === expected.byteLength;
// Replace the global fetch the SDK uses by default for the rest of a test.
const withGlobalFetch = async (fetch, run) => {
  const original = globalThis.fetch;
  globalThis.fetch = fetch;
  try {
    return await run();
  } finally {
    globalThis.fetch = original;
  }
};
const shapes = ["content", "grant", "multipart"];

test("with Node's fetch, upload() streams the caller's own bytes once with an explicit length", async () => {
  for (const shape of shapes) {
    const service = uploadService(shape);
    const bytes = new TextEncoder().encode("abc");
    const user = new OrganizationClient({
      baseUrl: "https://core.example",
      apiKey,
    }).user("user_1");
    const file = await withGlobalFetch(service.fetch, () =>
      user.files.upload({
        originalName: "note.txt",
        bytes,
        idempotencyKey: `stream-${shape}`,
      }),
    );
    assert.equal(file.status, "ready");
    const requests = service.byteRequests(bytes);
    assert.equal(requests.length, shape === "multipart" ? 2 : 1, shape);
    for (const { init, chunks, expected } of requests) {
      assert(init.body instanceof ReadableStream, shape);
      assert.equal(init.duplex, "half", shape);
      assert.equal(init.method, "PUT", shape);
      assert.equal(
        new Headers(init.headers).get("content-length"),
        String(expected.byteLength),
        shape,
      );
      // One chunk: the caller's array, or a view of it, neither copied nor detached.
      assert.equal(chunks.length, 1, shape);
      assert(sameView(chunks[0], expected), shape);
    }
    if (shape !== "content")
      for (const { init } of requests)
        assert.equal(new Headers(init.headers).get("authorization"), null);
    assert.equal(new TextDecoder().decode(bytes), "abc");
  }
});

test("with an injected fetch, upload() and the explicit steps send the caller's array itself", async () => {
  for (const shape of shapes) {
    const service = uploadService(shape);
    const bytes = new TextEncoder().encode("abc");
    const user = new OrganizationClient({
      baseUrl: "https://core.example",
      apiKey,
      fetch: service.fetch,
    }).user("user_1");
    await withGlobalFetch(
      () => assert.fail("the injected fetch carries every request"),
      () =>
        user.files.upload({
          originalName: "note.txt",
          bytes,
          idempotencyKey: `array-${shape}`,
        }),
    );
    const requests = service.byteRequests(bytes);
    assert.equal(requests.length, shape === "multipart" ? 2 : 1, shape);
    for (const { init, expected } of requests) {
      assert(sameView(init.body, expected), shape);
      assert.equal("duplex" in init, false, shape);
      assert.equal(
        new Headers(init.headers).get("content-length"),
        String(expected.byteLength),
        shape,
      );
    }
  }
  const service = uploadService("content");
  const files = new OrganizationClient({
    baseUrl: "https://core.example",
    apiKey,
    fetch: service.fetch,
  }).user("user_1").files;
  const content = new TextEncoder().encode("abc");
  await files.uploadContent("shaped", content);
  const part = new TextEncoder().encode("ab");
  await files.uploadPart(
    {
      number: 1,
      size: 2,
      url: "https://storage.example/part",
      headers: {
        "x-amz-checksum-sha256": Buffer.from(
          "fb8e20fc2e4c3f248c60c39bd652f3c1347298bb977b8b4d5903b85055620603",
          "hex",
        ).toString("base64"),
      },
      expiresAt: "2099-01-01T00:00:00.000Z",
    },
    part,
  );
  assert.equal(service.sent[0].init.body, content);
  assert.equal(
    service.sent[0].init.headers["content-digest"],
    "sha-256=:ungWv48Bz+pBQUDeXa4iI7ADYaOWF3qctBD/YfIAFa0=:",
  );
  assert.equal(service.sent[1].init.body, part);
  await assert.rejects(files.uploadContent("shaped", "abc"), /Uint8Array/);
  await assert.rejects(files.uploadPart({}, [1, 2]), /Uint8Array/);
  assert.equal(service.sent.length, 2);
});

test("a byte transfer that sent nothing fails as itself, not as an unknown outcome", async () => {
  // Nothing can listen on port 0, so every connection is refused.
  const closed = "http://127.0.0.1:0";
  const user = new OrganizationClient({ baseUrl: closed, apiKey }).user(
    "user_1",
  );
  // Node's fetch never reached the service, so it read none of the stream.
  await assert.rejects(
    user.files.uploadContent("inline", new TextEncoder().encode("abc")),
    (error) =>
      error.name === "ConnectionError" &&
      !(error instanceof OutcomeUnknownError),
  );
  const part = {
    number: 1,
    size: 3,
    url: closed + "/part",
    headers: {
      "x-amz-checksum-sha256": "ungWv48Bz+pBQUDeXa4iI7ADYaOWF3qctBD/YfIAFa0=",
    },
    expiresAt: "2099-01-01T00:00:00.000Z",
  };
  await assert.rejects(
    user.files.uploadPart(part, Buffer.from("abc")),
    (error) =>
      error.name === "ConnectionError" &&
      !(error instanceof OutcomeUnknownError),
  );
  // Grant headers fetch would refuse are a local TypeError.
  await assert.rejects(
    user.files.uploadPart(
      { ...part, headers: { ...part.headers, "x-bad": "a\nb" } },
      Buffer.from("abc"),
    ),
    TypeError,
  );

  // Bytes that change size before fetch reads them stop the body locally.
  const sent = new TextEncoder().encode("abc");
  const detaching = async (input, init) => {
    if (!(init.body instanceof ReadableStream))
      return Response.json({ file: ready("inline") });
    structuredClone(sent.buffer, { transfer: [sent.buffer] });
    await Array.fromAsync(init.body);
  };
  await withGlobalFetch(detaching, () =>
    assert.rejects(
      new OrganizationClient({ baseUrl: "https://core.example", apiKey })
        .user("user_1")
        .files.uploadContent("inline", sent),
      (error) =>
        error instanceof TypeError && /changed size/.test(error.message),
    ),
  );
  // An injected fetch cannot show what it sent, so the outcome stays unknown.
  await assert.rejects(
    new OrganizationClient({
      baseUrl: "https://core.example",
      apiKey,
      fetch: async () => {
        throw new TypeError("fetch failed");
      },
    })
      .user("user_1")
      .files.uploadContent("inline", new TextEncoder().encode("abc")),
    OutcomeUnknownError,
  );

  // Once the service may have received the body, the outcome is unknown.
  const dropping = await serve(async (req) => {
    await readBytes(req);
    req.socket.destroy();
  });
  await assert.rejects(
    new OrganizationClient({ baseUrl: dropping, apiKey })
      .user("user_1")
      .files.uploadContent("inline", new TextEncoder().encode("abc")),
    OutcomeUnknownError,
  );
  await assert.rejects(
    new OrganizationClient({ baseUrl: dropping, apiKey })
      .user("user_1")
      .files.uploadPart(
        { ...part, url: dropping + "/part" },
        Buffer.from("abc"),
      ),
    OutcomeUnknownError,
  );
});

test("upload() transfers that sent nothing carry the upload and key, apart from unknown outcomes", async () => {
  const abort = new AbortController();
  // Each case fails the byte transfer of an inline upload after creation.
  const cases = [
    {
      // Fetch failed before reading the body: nothing was sent.
      transfer: async () => {
        throw new TypeError("fetch failed");
      },
      check: (error) =>
        error instanceof FileUploadError &&
        error.cause instanceof ConnectionError,
    },
    {
      transfer: async () => {
        abort.abort();
        throw abort.signal.reason;
      },
      signal: abort.signal,
      check: (error) =>
        error instanceof FileUploadError && error.cause.name === "AbortError",
    },
    {
      // Fetch read the body before failing: the service may have it.
      transfer: async (init) => {
        await Array.fromAsync(init.body);
        throw new TypeError("fetch failed");
      },
      check: (error) =>
        error instanceof FileUploadOutcomeUnknownError &&
        !(error instanceof FileUploadError),
    },
  ];
  for (const { transfer, check, signal } of cases) {
    const service = uploadService("content");
    const fetch = (input, init) =>
      String(input).endsWith("/content")
        ? transfer(init)
        : service.fetch(input, init);
    await withGlobalFetch(fetch, () =>
      assert.rejects(
        new OrganizationClient({ baseUrl: "https://core.example", apiKey })
          .user("user_1")
          .files.upload({
            originalName: "note.txt",
            bytes: new TextEncoder().encode("abc"),
            idempotencyKey: "known-key",
            ...(signal ? { signal } : {}),
          }),
        (error) =>
          check(error) &&
          error.uploadId === "shaped" &&
          error.idempotencyKey === "known-key",
      ),
    );
  }
  // A refusal of the transfer is the service's error, as before.
  const service = uploadService("content");
  await withGlobalFetch(
    (input, init) =>
      String(input).endsWith("/content")
        ? Response.json(
            { error: { code: "drive_upload_refused", message: "No" } },
            { status: 422 },
          )
        : service.fetch(input, init),
    () =>
      assert.rejects(
        new OrganizationClient({ baseUrl: "https://core.example", apiKey })
          .user("user_1")
          .files.upload({
            originalName: "note.txt",
            bytes: new TextEncoder().encode("abc"),
            idempotencyKey: "refused",
          }),
        (error) =>
          error.constructor === TiDBLinkApiError &&
          error.code === "drive_upload_refused",
      ),
  );
  // A failure before the upload exists is the original error.
  await withGlobalFetch(
    async () => {
      throw new TypeError("fetch failed");
    },
    () =>
      assert.rejects(
        new OrganizationClient({ baseUrl: "https://core.example", apiKey })
          .user("user_1")
          .files.upload({
            originalName: "note.txt",
            bytes: new TextEncoder().encode("abc"),
            idempotencyKey: "no-upload",
          }),
        (error) =>
          error instanceof FileUploadOutcomeUnknownError &&
          error.uploadId === undefined,
      ),
  );
});

test("bytes that change size after hashing, or are shared, are never sent", async () => {
  for (const injected of [false, true]) {
    for (const shape of shapes) {
      const service = uploadService(shape);
      const bytes = new TextEncoder().encode("abc");
      // The caller transfers its buffer away while the upload is being created.
      const fetch = async (input, init) => {
        if (String(input).endsWith("/uploads"))
          structuredClone(bytes.buffer, { transfer: [bytes.buffer] });
        return service.fetch(input, init);
      };
      const upload = () =>
        new OrganizationClient({
          baseUrl: "https://core.example",
          apiKey,
          ...(injected ? { fetch } : {}),
        })
          .user("user_1")
          .files.upload({
            originalName: "note.txt",
            bytes,
            idempotencyKey: `detached-${shape}`,
          });
      await withGlobalFetch(fetch, () =>
        assert.rejects(
          upload(),
          (error) =>
            error instanceof FileUploadError &&
            error.cause instanceof TypeError &&
            /changed size/.test(error.cause.message),
        ),
      );
      assert.deepEqual(
        service.byteRequests(bytes).map((r) => r.url),
        [],
        `${shape} ${injected}`,
      );
    }

    const service = uploadService("content");
    const files = new OrganizationClient({
      baseUrl: "https://core.example",
      apiKey,
      ...(injected ? { fetch: service.fetch } : {}),
    }).user("user_1").files;
    const shared = new Uint8Array(new SharedArrayBuffer(3));
    await withGlobalFetch(service.fetch, async () => {
      await assert.rejects(
        files.upload({
          originalName: "note.txt",
          bytes: shared,
          idempotencyKey: "shared",
        }),
        /SharedArrayBuffer/,
      );
      await assert.rejects(
        files.uploadContent("shaped", shared),
        /SharedArrayBuffer/,
      );
      await assert.rejects(
        files.uploadPart(
          {
            number: 1,
            size: 3,
            url: "https://storage.example/part",
            headers: {},
            expiresAt: "2099-01-01T00:00:00.000Z",
          },
          shared,
        ),
        /SharedArrayBuffer/,
      );
    });
    assert.equal(service.sent.length, 0);
  }
});
