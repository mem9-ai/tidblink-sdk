import {
  ConflictError,
  ConnectionError,
  DriveFileVersionConflictError,
  TiDBLinkError,
} from "./errors.js";
import { fetchDownload } from "./files.js";
import {
  apiErrorFrom,
  HttpClient,
  isByteSize,
  mediaType,
  requireKnownKeys,
} from "./http.js";
import type { RequestOptions } from "./organization.js";

export interface SessionDriveFileReadOptions extends RequestOptions {
  /**
   * The `version` a File Notification announced. With it, only that version
   * is read, and a file changed since then raises
   * `DriveFileVersionConflictError`. Without it, the current bytes are read.
   */
  version?: string;
}

/**
 * A short-lived download ticket for one file in a Session's Drive. Its holder,
 * such as a device, downloads the file from `url` without an API Key until
 * `expiresAt`. It is a bearer credential: do not log or store it.
 */
export interface SessionDriveFileTicket {
  /** Absolute URL, resolved against the client's `baseUrl`. */
  url: string;
  expiresAt: string;
  byteSize: number;
}

const DOWNLOAD_TICKET_TYPE = "application/vnd.tidb-link.file-download+json";
/** The service's code for a file no longer at the version a read is pinned to. */
const VERSION_CONFLICT = "drive_file_version_conflict";

/**
 * Files in one Session's Drive, read by path, such as the path a File
 * Notification announced. Reads use the authority of writing Session files.
 */
export class SessionDriveFiles {
  readonly #http: HttpClient;
  readonly #sessionId: string;

  constructor(http: HttpClient, sessionId: string) {
    this.#http = http;
    this.#sessionId = sessionId;
  }

  /**
   * The file's bytes, held in memory. A file above the service's inline
   * limit (50 MiB) is downloaded through the ticket the service answers with.
   */
  async read(
    path: string,
    options: SessionDriveFileReadOptions = {},
  ): Promise<Uint8Array> {
    const { version, ...request } = validateFileRead(path, options);
    const query = new URLSearchParams({ path });
    if (version !== undefined) query.set("version", version);
    const { response, requestId } = await withVersionConflict(
      this.#http.rawResponse(`${this.#path}/content?${query}`, {
        ...request,
        retry: "safe",
        headers: { accept: "*/*" },
      }),
    );
    if (mediaType(response) !== DOWNLOAD_TICKET_TYPE)
      return bytesOf(response, requestId);
    let value: unknown;
    try {
      value = await response.json();
    } catch {
      value = undefined;
    }
    // The service enforces the ticket's expiry; a client clock may be wrong.
    const ticket = this.#ticket(value, requestId);
    const download = await fetchDownload(this.#http, ticket.url, request.signal);
    if (!download.ok)
      throw versionConflictFrom(await apiErrorFrom(download, requestId));
    const bytes = await bytesOf(download, requestId);
    if (bytes.byteLength !== ticket.byteSize)
      throw new TiDBLinkError(
        "Downloaded Session file does not match its ticket's size",
        { requestId },
      );
    return bytes;
  }

  /**
   * A 60-second download ticket for the file, to hand to a device or another
   * process that holds no API Key. Pinned to `version`, its download fails
   * with a version conflict once the file has changed.
   */
  async createDownloadTicket(
    path: string,
    options: SessionDriveFileReadOptions = {},
  ): Promise<SessionDriveFileTicket> {
    const { version, ...request } = validateFileRead(path, options);
    const result = await withVersionConflict(
      this.#http.postResponse<unknown>(
        `${this.#path}/download-tickets`,
        version === undefined ? { path } : { path, version },
        request,
      ),
    );
    return this.#ticket(result.data, result.requestId);
  }

  #ticket(value: unknown, requestId: string): SessionDriveFileTicket {
    const ticket = value as Partial<SessionDriveFileTicket> | null | undefined;
    if (
      typeof ticket?.url !== "string" ||
      !ticket.url ||
      !isByteSize(ticket.byteSize) ||
      typeof ticket.expiresAt !== "string" ||
      !Number.isFinite(Date.parse(ticket.expiresAt))
    )
      throw new TiDBLinkError(
        "Session file returned no usable download ticket",
        { requestId },
      );
    return {
      url: this.#http.resolveUrl(ticket.url).toString(),
      expiresAt: ticket.expiresAt,
      byteSize: ticket.byteSize,
    };
  }

  get #path(): string {
    return `/sessions/${encodeURIComponent(this.#sessionId)}/files`;
  }
}

/** The service's version conflict as `DriveFileVersionConflictError`; any other error unchanged. */
const versionConflictFrom = (error: unknown): unknown =>
  error instanceof ConflictError &&
  !(error instanceof DriveFileVersionConflictError) &&
  error.status === 409 &&
  error.code === VERSION_CONFLICT
    ? new DriveFileVersionConflictError({
        status: error.status,
        code: error.code,
        message: error.message,
        ...(error.requestId === undefined ? {} : { requestId: error.requestId }),
        ...(error.details === undefined ? {} : { details: error.details }),
        ...(error.retryAfter === undefined
          ? {}
          : { retryAfter: error.retryAfter }),
      })
    : error;

/** The request, with its version conflict as `DriveFileVersionConflictError`. */
const withVersionConflict = <T>(request: Promise<T>): Promise<T> =>
  request.catch((error: unknown) => {
    throw versionConflictFrom(error);
  });

const bytesOf = async (
  response: Response,
  requestId: string,
): Promise<Uint8Array> => {
  try {
    return new Uint8Array(await response.arrayBuffer());
  } catch {
    throw new ConnectionError(
      "Session file download ended before the complete body arrived",
      { requestId },
    );
  }
};

/** Check a read's or ticket's path and options before anything is sent. */
const validateFileRead = (
  path: unknown,
  options: SessionDriveFileReadOptions,
): SessionDriveFileReadOptions => {
  requireKnownKeys(
    options,
    ["version", "signal", "requestId"],
    "Session file options",
  );
  if (typeof path !== "string" || !path)
    throw new TypeError("A Session file path must be non-empty text");
  // The version is opaque: its form is the service's to judge.
  if (
    options.version !== undefined &&
    (typeof options.version !== "string" || !options.version)
  )
    throw new TypeError(
      "A Session file version must be the non-empty text a File Notification announced",
    );
  return { ...options };
};
