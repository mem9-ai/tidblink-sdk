import { pathToFileURL } from "node:url";
import { OrganizationClient } from "@tidblink/server";

/**
 * A cloud capability served from this backend: the Organization's `orders`
 * Connector, whose functions any Session calls when its Template references
 * the Connector. Calls arrive over this process's Backend Connections, so the
 * backend needs no public endpoint.
 *
 * `orders` is your own data access: `listOrders(userId, status)` and
 * `getOrder(userId, orderId)`. Each call names the End User whose Session made
 * it; answer only with that user's data. The model sees the functions as
 * `mcp_orders__list_orders` and `mcp_orders__get_order`.
 *
 * Returns the served Connector: put `connectorId` in the Template's
 * `tools.mcp[].serverId`, and `close()` it on shutdown.
 */
export function serveOrdersConnector({ organization, orders, name = "orders", onError }) {
  return organization.serveConnector({
    name,
    tools: {
      list_orders: {
        description: "List the current user's orders, optionally only those with one status",
        parameters: {
          type: "object",
          properties: {
            status: { type: "string", enum: ["open", "shipped", "delivered"] },
          },
          additionalProperties: false,
        },
        run: async ({ status }, { userId, signal }) =>
          JSON.stringify(await orders.listOrders(userId, status, { signal })),
      },
      get_order: {
        description: "Read one of the current user's orders",
        parameters: {
          type: "object",
          properties: { orderId: { type: "string" } },
          required: ["orderId"],
          additionalProperties: false,
        },
        run: async ({ orderId }, { userId, signal }) => {
          const order = await orders.getOrder(userId, orderId, { signal });
          // The model sees the message of a thrown error.
          if (!order) throw new Error(`Order ${orderId} was not found`);
          return JSON.stringify(order);
        },
      },
    },
    ...(onError ? { onError } : {}),
  });
}

/** Sample orders in memory; use your own database. */
export function memoryOrders(byUser = {}) {
  const of = (userId) => byUser[userId] ?? [];
  return {
    listOrders: async (userId, status) =>
      of(userId).filter((order) => !status || order.status === status),
    getOrder: async (userId, orderId) => of(userId).find((order) => order.orderId === orderId),
  };
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const organization = new OrganizationClient({
    baseUrl: process.env.AGENT_SERVICE_URL,
    apiKey: process.env.ORGANIZATION_API_KEY,
  });
  try {
    const connector = await serveOrdersConnector({
      organization,
      orders: memoryOrders(),
      name: process.env.CONNECTOR_NAME || "orders",
      onError: (error) => console.error(error),
    });
    console.log(JSON.stringify({ connectorId: connector.connectorId }));
    const stop = () => void organization.close();
    process.once("SIGINT", stop);
    process.once("SIGTERM", stop);
    const end = await connector.closed;
    if (end.reason === "error") throw end.error;
  } catch (error) {
    console.error(error instanceof Error ? error.message : "Serving the Connector failed");
    process.exitCode = 1;
    await organization.close();
  }
}
