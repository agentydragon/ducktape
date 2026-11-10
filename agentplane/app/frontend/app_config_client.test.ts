// @vitest-environment happy-dom
import { afterEach, expect, it, vi } from "vitest";

import { getFrontendConfig, getSandboxForm, listModels, listSandboxPresets } from "./app_config_client";
import { Harness } from "../../proto/session_pb";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

it("decodes the generated AppService ProtoJSON responses", async () => {
  const paths: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const path = new URL(input instanceof Request ? input.url : String(input), "http://agentplane.test").pathname;
      paths.push(path);
      if (path.endsWith("/models")) {
        return Response.json({
          models: [{ model: "model-a", displayName: "Model A", reasoningEfforts: ["low", "high"] }],
          harnesses: [
            { harness: "HARNESS_CODEX", models: ["model-a"] },
            { harness: "HARNESS_CLAUDE", models: [] },
          ],
        });
      }
      if (path.endsWith("/frontend-config")) {
        return Response.json({
          frontendConfig: {
            traceSearch: {
              grafanaBaseUrl: "https://grafana.example.test/",
              tempoDatasourceUid: "fixture-tempo",
              deploymentEnvironment: "fixture",
            },
            langfuseSearch: {
              baseUrl: "https://langfuse.example.test/",
              projectId: "fixture-project",
              sessionMetadataKey: "session",
              sandboxNamespaceMetadataKey: "namespace",
              sandboxServiceAccountMetadataKey: "account",
            },
          },
        });
      }
      if (path.endsWith("/sandbox-form")) {
        return Response.json({
          policySets: ["alpha", "zeta"],
          newSandboxDefaults: { wakeOnSend: true, idleSuspendAfterSeconds: "900" },
        });
      }
      return Response.json({
        presets: [
          {
            name: "work",
            title: "Work",
            template: "linux",
            policySets: ["baseline"],
            inboxTags: [],
            sessionDefaults: {
              harness: "HARNESS_CODEX",
              model: "model-a",
              cwd: "/workspace",
              reasoningEffort: "medium",
              instructions: "",
              setupScript: "echo ready",
            },
            bootstrap: "mkdir -p /workspace",
          },
        ],
      });
    })
  );

  const catalog = await listModels();
  expect(
    catalog.models.map(({ model, displayName, reasoningEfforts }) => ({ model, displayName, reasoningEfforts }))
  ).toEqual([{ model: "model-a", displayName: "Model A", reasoningEfforts: ["low", "high"] }]);
  expect(catalog.harnesses.map(({ harness, models }) => [harness, models])).toEqual([
    [Harness.CODEX, ["model-a"]],
    [Harness.CLAUDE, []],
  ]);
  const config = await getFrontendConfig();
  expect(config.traceSearch?.grafanaBaseUrl).toBe("https://grafana.example.test/");
  expect(config.langfuseSearch?.sandboxServiceAccountMetadataKey).toBe("account");
  const [preset] = await listSandboxPresets();
  expect(preset?.sessionDefaults?.harness).toBe(Harness.CODEX);
  expect(preset?.sessionDefaults?.reasoningEffort).toBe("medium");
  const form = await getSandboxForm();
  expect(form.policySets).toEqual(["alpha", "zeta"]);
  expect(form.newSandboxDefaults.idleSuspendAfterSeconds).toBe(900n);
  expect(paths).toEqual([
    "/upstream/app-config/v1/models",
    "/upstream/app-config/v1/frontend-config",
    "/upstream/app-config/v1/presets",
    "/upstream/app-config/v1/sandbox-form",
  ]);
});

it("maps unset ProtoJSON message fields to absent optional links", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => Response.json({ frontendConfig: { traceSearch: null, langfuseSearch: null } }))
  );
  const config = await getFrontendConfig();
  expect(config.traceSearch).toBeUndefined();
  expect(config.langfuseSearch).toBeUndefined();
});

it.each([
  { label: "models", read: listModels, wire: '{"harnesses":[{"harness":"HARNESS_UNSPECIFIED"}]}' },
  { label: "presets without session defaults", read: listSandboxPresets, wire: '{"presets":[{"name":"no-defaults"}]}' },
])("rejects $label that name no known harness", async ({ read, wire }) => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(wire))
  );
  await expect(read()).rejects.toThrow("unknown harness");
});

it.each([
  { wire: '{"code":10,"message":"changed"}', body: { code: 10, message: "changed" }, code: 10 },
  { wire: "gateway unavailable", body: "gateway unavailable", code: undefined },
])("preserves HTTP error details before protobuf decoding: $wire", async ({ wire, body, code }) => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(wire, { status: 409 }))
  );

  await expect(listModels()).rejects.toMatchObject({ status: 409, code, body });
});
