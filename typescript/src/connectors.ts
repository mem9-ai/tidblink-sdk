import { HttpClient, requestOptions } from "./http.js";
import type { RequestOptions } from "./organization.js";

/**
 * How the platform reaches a Connector. `backend_connection` Connectors are
 * the ones Application Servers serve with `serveConnector`; the others are
 * older registrations, which the SDK only reads.
 */
export type ConnectorTransport = "backend_connection" | "streamable_http" | "tunnel";

/** An Organization-owned Connector: functions that Sessions call as `mcp_<name>__<function>`. */
export interface Connector {
  connectorId: string;
  organizationId: string;
  displayName: string;
  /** The HTTPS MCP endpoint of a `streamable_http` Connector; empty otherwise. */
  endpointUrl: string;
  endpointVersion: number;
  transport: ConnectorTransport;
  /** A `backend_connection` Connector is active from its first declaration. */
  status: "inactive" | "active";
  connectorVersion: number;
  createdAt: string;
  updatedAt: string;
  /** What activation discovered at a `streamable_http` endpoint; null otherwise. */
  observation: {
    era: "modern" | "legacy";
    protocolVersion: string;
    endpointVersion: number;
    probedAt: string;
    expiresAt: string;
  } | null;
}

const connectorPath = (id: string) => `/connectors/${encodeURIComponent(id)}`;

/**
 * Reads the Organization's Connectors. Serving a Connector with
 * `organization.serveConnector()` creates it; there is no separate
 * registration.
 */
export class Connectors {
  readonly #http: HttpClient;

  constructor(http: HttpClient) {
    this.#http = http;
  }

  async list(options?: RequestOptions): Promise<Connector[]> {
    const response = await this.#http.get<{ connectors: Connector[] }>(
      "/connectors",
      requestOptions(options),
    );
    return response.connectors;
  }

  get(connectorId: string, options?: RequestOptions): Promise<Connector> {
    return this.#http.get(connectorPath(connectorId), requestOptions(options));
  }
}
