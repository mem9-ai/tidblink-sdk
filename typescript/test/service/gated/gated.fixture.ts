import { execFile } from "node:child_process";
import { copyFile, mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { promisify } from "node:util";
import { randomUUID } from "node:crypto";
import { afterAll, beforeAll, it } from "vitest";
import type { NestFastifyApplication } from "@nestjs/platform-fastify";
import {
  AppTestingPersistenceFixture,
  createAppTestingModule,
  getAppPersistencePorts,
} from "../test/support/app-testing-module";
import { loadE2eEnv } from "../test/support/e2e-env";
import { OrganizationApiKeyService } from "../src/auth/organization-api-key.service";
import { seedFixtureTemplate } from "../test/support/published-template-fixture";
import { AgentTemplateContentSchema } from "../src/agent-templates/agent-template-content";
import { createAgentServiceFastifyAdapter } from "../src/common/fastify-adapter";
import { ENV } from "../src/config/config.module";
import { DEFAULT_ORG_ID } from "../src/chat/dto";
import {
  developmentReleaseServices,
  opsFixtureOwner,
} from "../test/support/ops-release-fixture";
import { OpsAuthorityStore } from "../src/ops/ops-authority.store";
import { OpsFeatureAdmissionStore } from "../src/ops/ops-feature-admission.store";
import { AGENT_MODEL_NAMES } from "../src/agent-models/agent-model-registry";

// Gated features: one Organization is admitted through Ops Feature Admission, the other is not.
const exec = promisify(execFile);
const CLOSED_ORG = "org_sdk_gated_closed";
let app: NestFastifyApplication;
let temporary: string;

beforeAll(async () => {
  temporary = await mkdtemp(path.join(tmpdir(), "sdk-gated-service-"));
  const releaseServices = developmentReleaseServices({
    models: [
      {
        provider: "openai_compatible",
        endpoint: "https://example.test/v1",
        apiMode: "responses",
        modelIds: AGENT_MODEL_NAMES,
        apiKey: "fictional-gated-model-key",
      },
    ],
  });
  const moduleRef = await createAppTestingModule({ releaseServices })
    .overrideProvider(ENV)
    .useValue(
      loadE2eEnv({
        NODE_ENV: "test",
        OPS_SERVICE_ALLOWED_HOSTS: "example.test",
        WORKSPACE_ROOT: path.join(temporary, "workspace"),
        WORKSPACE_KEK: "fictional-gated-kek",
        SESSION_SECRET: "fictional-gated-session",
        OPS_SESSION_SECRET: "fictional-gated-ops-session",
      }),
    )
    .compile();
  app = moduleRef.createNestApplication<NestFastifyApplication>(
    createAgentServiceFastifyAdapter(),
  );
  await app.init();
  await app.listen(0, "127.0.0.1");
}, 120000);

afterAll(async () => {
  await app?.close();
  if (temporary) await rm(temporary, { recursive: true, force: true });
});

/** Admit the Organization to `feature` as an Ops operator would, unless it already is. */
const admit = async (feature: string, organizationId: string) => {
  const store = app.get(OpsFeatureAdmissionStore);
  const owner = await opsFixtureOwner(app.get(OpsAuthorityStore));
  const current = await store.organization(owner, feature, organizationId);
  if (current.enabled) return;
  await store.append(owner, feature, organizationId, {
    enabled: true,
    reason: "SDK gated feature fixture",
    expectedRevision: current.revision,
  });
};

const arrangeTemplate = async (organizationId: string, name: string) => {
  const templateId = `template_${randomUUID().replaceAll("-", "")}`;
  await seedFixtureTemplate(getAppPersistencePorts(app).templateStore, {
    organizationId,
    templateId,
    name,
  });
  await app.get(AppTestingPersistenceFixture).arrangeAgentTemplateContent({
    organizationId,
    templateId,
    content: AgentTemplateContentSchema.parse({
      realtime: null,
      identity: { agentName: name },
      sandbox: { profileId: "standard-v1" },
    }),
  });
  return templateId;
};

it("the installed SDK manages Agents through the actual gate, persistence and Session creation", async () => {
  const fixture = app.get(AppTestingPersistenceFixture);
  const ports = getAppPersistencePorts(app);
  await ports.organizationStore.seedOrganization({
    organizationId: CLOSED_ORG,
    slug: "org-sdk-gated-closed",
    name: "SDK gated closed",
  });
  const keys = app.get(OrganizationApiKeyService);
  const [openKey, closedKey] = await Promise.all(
    [DEFAULT_ORG_ID, CLOSED_ORG].map((organizationId) =>
      keys.create({ organizationId, name: "SDK gated fixture", createdByUserId: null }),
    ),
  );
  const [owner, stranger, closedOwner] = await Promise.all([
    fixture.arrangeEndUser({ organizationId: DEFAULT_ORG_ID, displayName: "owner" }),
    fixture.arrangeEndUser({ organizationId: DEFAULT_ORG_ID, displayName: "stranger" }),
    fixture.arrangeEndUser({ organizationId: CLOSED_ORG, displayName: "closed" }),
  ]);
  const templateId = await arrangeTemplate(DEFAULT_ORG_ID, "SDK Gated");
  const secondTemplateId = await arrangeTemplate(DEFAULT_ORG_ID, "SDK Gated Second");
  await admit("agent_preview", DEFAULT_ORG_ID);

  const consumer = path.join(temporary, "consumer");
  await mkdir(consumer);
  const packed = JSON.parse(
    (
      await exec("npm", ["pack", "--json", "--pack-destination", consumer], {
        cwd: process.env.SDK_PACKAGE_ROOT,
      })
    ).stdout,
  )[0];
  await writeFile(
    path.join(consumer, "package.json"),
    JSON.stringify({ private: true, type: "module" }),
  );
  await exec(
    "npm",
    ["install", "--ignore-scripts", "--no-audit", "--no-fund", path.join(consumer, packed.filename)],
    { cwd: consumer },
  );
  const cases = JSON.parse(process.env.SDK_SERVICE_CASES!) as Array<{
    source: string;
    name: string;
  }>;
  for (const entry of cases) {
    const destination = path.join(consumer, entry.name);
    await mkdir(path.dirname(destination), { recursive: true });
    await copyFile(entry.source, destination);
  }
  try {
    const result = await exec(
      process.execPath,
      ["--test", "--test-concurrency=1", ...cases.map((entry) => entry.name)],
      {
        cwd: consumer,
        env: {
          ...process.env,
          SDK_SERVICE_URL: await app.getUrl(),
          SDK_ORGANIZATION_KEY: openKey.token,
          SDK_USER_ID: owner!.userId,
          SDK_FOREIGN_USER_ID: stranger!.userId,
          SDK_TEMPLATE_ID: templateId,
          SDK_SECOND_TEMPLATE_ID: secondTemplateId,
          SDK_CLOSED_ORGANIZATION_KEY: closedKey.token,
          SDK_CLOSED_USER_ID: closedOwner!.userId,
        },
        maxBuffer: 4 * 1024 * 1024,
      },
    );
    console.log(result.stdout);
  } catch (error) {
    const output = error as Error & { stdout?: string; stderr?: string };
    throw new Error(
      `SDK gated service cases failed:\n${output.stdout ?? ""}${output.stderr ?? ""}`,
    );
  }
}, 120000);
