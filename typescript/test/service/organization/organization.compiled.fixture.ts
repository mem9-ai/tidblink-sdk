import { execFile, fork, type ChildProcess } from "node:child_process";
import { copyFile, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { promisify } from "node:util";
import { expect, it } from "vitest";
import { startSystemHarness } from "../test/system/support/system-harness";
import { SYSTEM_HARNESS_OPS_OWNER } from "../test/system/support/system-harness";
import { publishOpsReleaseOverHttp } from "../test/system/support/ops-release-http";
import {
  AGENT_MODEL_NAMES,
  DEFAULT_AGENT_MODEL,
} from "../src/agent-models/agent-model-registry";
import { parseApiKeyTokenId } from "../src/auth/api-key-token";

const exec = promisify(execFile);

it("an independent installed SDK consumer uses Organization authority against the compiled backend", async () => {
  const harness = await startSystemHarness({
    databaseAdminUrl: process.env.TEST_TIDB_DATABASE_URL!,
    tidbSsl: process.env.TIDB_SSL ?? "off",
    controlledDrive9: true,
    controlledAudioTranscription: true,
    controlledVoiceProvider: true,
  });
  const temporary = await mkdtemp(
    path.join(tmpdir(), "tidb-link-org-consumer-"),
  );
  let child: ChildProcess | undefined;
  try {
    await publishOpsReleaseOverHttp({
      baseUrl: harness.baseUrl,
      owner: SYSTEM_HARNESS_OPS_OWNER,
      services: {
        models: [
          {
            provider: "openai_compatible",
            endpoint: harness.model.openAi.baseUrl,
            apiMode: "chat_completions",
            modelIds: AGENT_MODEL_NAMES,
            apiKey: harness.model.openAi.apiKey,
          },
        ],
        defaultModel: DEFAULT_AGENT_MODEL,
        sandbox: { provider: "local" },
        drive9: {
          server: harness.integrations.baseUrl,
          provisionMode: "anonymous",
        },
      },
      realtime: {
        apiKey: "sk-controlled-sdk-voice",
        defaultModel: "voice-a",
        parameters: {
          provider: "bailian_qwen_omni",
          region: "cn-beijing",
          workspaceId: "ws-sdk-controlled",
          models: [
            {
              realtimeModelId: "voice-a",
              label: "Controlled voice",
              model: "qwen3.5-omni-plus-realtime",
              voice: null,
              turnDetection: null,
              vadThreshold: null,
              vadSilenceDurationMs: null,
            },
          ],
        },
      },
    });
    const organizationId = harness.fixtures.organization.id;
    const token = await harness.issueOrganizationCredential({
      organizationId,
      createdByUserId: null,
    });
    const other = await harness.issueOrganizationCredential({
      organizationId,
      createdByUserId: null,
    });
    const template = await harness.publishAgentTemplateFixture({
      name: "SDK Organization journey",
      content: { realtime: { language: "en-US" } },
    });
    const packed = JSON.parse(
      (
        await exec(
          "npm",
          ["pack", "--json", "--pack-destination", temporary],
          {
            cwd: process.env.SDK_PACKAGE_ROOT,
          },
        )
      ).stdout,
    )[0];
    await writeFile(
      path.join(temporary, "package.json"),
      JSON.stringify({ private: true, type: "module" }),
    );
    await exec(
      "npm",
      [
        "install",
        "--ignore-scripts",
        "--no-audit",
        "--no-fund",
        path.join(temporary, packed.filename),
      ],
      { cwd: temporary },
    );
    const [{ source }] = JSON.parse(process.env.SDK_SERVICE_CASES!);
    await copyFile(source, path.join(temporary, "consumer.mjs"));
    child = fork(path.join(temporary, "consumer.mjs"), [], {
      cwd: temporary,
      stdio: ["ignore", "pipe", "pipe", "ipc"],
      env: {
        ...process.env,
        SDK_SERVICE_URL: harness.baseUrl,
        SDK_ORGANIZATION_KEY: token,
        SDK_OTHER_ORGANIZATION_KEY: other,
        SDK_TEMPLATE_ID: template.templateId,
      },
    });
    let output = "";
    child.stdout!.on("data", (bytes) => {
      output += bytes;
    });
    child.stderr!.on("data", (bytes) => {
      output += bytes;
    });
    let paused: ReturnType<typeof harness.model.enqueueText> | undefined;
    let connectorTitle: ReturnType<typeof harness.model.enqueueText> | undefined;
    const consumer = child;
    child.on("message", (message: { stage: string; userId?: string }) => {
      void (async () => {
        let value: unknown;
        if (message.stage === "storage") {
          await harness.promoteControlledUserDrive({
            organizationId,
            userId: message.userId!,
          });
        } else if (message.stage === "text") {
          // The Session was renamed first, so its first Turn requests no title.
          harness.model.enqueueText({
            chunks: ["Organization SDK answer."],
          });
        } else if (message.stage === "tools") {
          harness.model.enqueueToolCalls({
            toolCalls: [
              { id: "sdk_order", name: "lookup_order", arguments: {} },
            ],
          });
          harness.model.enqueueText({
            chunks: ["Order shipped."],
            expectRequest: { bodyIncludes: ["SDK_ORDER_SHIPPED"] },
          });
        } else if (message.stage === "connector") {
          // Templates reference Connectors through Console-only tool settings.
          value = (
            await harness.publishAgentTemplateFixture({
              name: "SDK Connector journey",
              content: {
                realtime: { language: "en-US" },
                tools: { mcp: [{ serverId: message.userId! }] },
              },
            })
          ).templateId;
          harness.model.enqueueToolCalls({
            toolCalls: [
              {
                id: "sdk_connector_call",
                name: "mcp_sdk_orders__get_order",
                arguments: { orderId: "order_7" },
              },
            ],
          });
          harness.model.enqueueText({
            chunks: ["Connector order shipped."],
            expectRequest: { bodyIncludes: ["SDK_CONNECTOR_RESULT"] },
          });
          // The new Session's first Turn also requests a background title.
          connectorTitle = harness.model.enqueueText({
            chunks: ["SDK Connector journey"],
            expectRequest: {
              bodyIncludes: ["Generate exactly one concise session title."],
            },
          });
        } else if (message.stage === "connector_offline") {
          // The background title of the Connector Session's first Turn came first.
          await connectorTitle?.waitUntilRequested();
          harness.model.enqueueToolCalls({
            toolCalls: [
              {
                id: "sdk_connector_offline_call",
                name: "mcp_sdk_orders__get_order",
                arguments: { orderId: "order_8" },
              },
            ],
          });
          harness.model.enqueueText({
            chunks: ["Connector unavailable."],
            expectRequest: { bodyIncludes: ["unavailable"] },
          });
        } else if (message.stage === "audio") {
          await connectorTitle?.waitUntilRequested();
          harness.model.enqueueText({ chunks: ["SDK audio answer."] });
        } else if (message.stage === "pause") {
          paused = harness.model.enqueueText({
            chunks: ["Accepted work survived rotation."],
            pauseBeforeResponse: true,
          });
        } else if (message.stage === "rotate") {
          await paused!.waitUntilPaused();
          value = (
            await harness.rotateOrganizationCredential(
              organizationId,
              parseApiKeyTokenId("organization", token)!,
            )
          ).token;
          paused!.release();
        } else if (message.stage === "revoke") {
          await harness.revokeOrganizationCredential(
            organizationId,
            parseApiKeyTokenId("organization", token)!,
          );
        } else throw new Error(`Unknown fixture stage: ${message.stage}`);
        consumer.send({ value });
      })().catch((error) =>
        consumer.send({ error: String(harness.redactError(error)) }),
      );
    });
    const code = await new Promise<number | null>((resolve, reject) => {
      consumer.once("error", reject);
      consumer.once("exit", resolve);
    });
    expect(code, String(harness.redactError(new Error(output)))).toBe(0);
    console.log(output);
  } finally {
    if (child && child.exitCode === null) child.kill();
    await rm(temporary, { recursive: true, force: true });
    const teardown = await harness.stop();
    expect(teardown.errors).toEqual([]);
  }
}, 180_000);
