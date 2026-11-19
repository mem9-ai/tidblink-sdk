import { HttpClient, requestOptions } from "./http.js";
import type { RequestOptions } from "./organization.js";

/**
 * An Organization's Connector, declared by `serveConnector` on a Backend
 * Connection: functions that Sessions call as `mcp_<name>__<function>`.
 */
export interface Connector {
  connectorId: string;
  /** The Connector's name, as `serveConnector` declared it. */
  displayName: string;
  createdAt: string;
  updatedAt: string;
}

const connectorPath = (id: string) => `/connectors/${encodeURIComponent(id)}`;

/**
 * Reads the Organization's Connectors: every Connector declared with
 * `organization.serveConnector()`, whether or not a process serves it now.
 * Serving a Connector creates it; there is no separate registration.
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
