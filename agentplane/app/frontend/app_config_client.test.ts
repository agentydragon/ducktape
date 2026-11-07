// @vitest-environment happy-dom
import { afterEach, expect, it, vi } from "vitest";

import { getFrontendConfig, listModels, listSandboxPresets } from "./app_config_client";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

it("decodes the generated AppService ProtoJSON responses into the UI config views", async () => {
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

  await expect(listModels()).resolves.toEqual({
    models: [{ model: "model-a", display_name: "Model A", reasoning_efforts: ["low", "high"] }],
    harnesses: { HARNESS_CODEX: ["model-a"], HARNESS_CLAUDE: [] },
  });
  await expect(getFrontendConfig()).resolves.toEqual({
    trace_search: {
      grafana_base_url: "https://grafana.example.test/",
      tempo_datasource_uid: "fixture-tempo",
      deployment_environment: "fixture",
    },
    langfuse_search: {
      base_url: "https://langfuse.example.test/",
      project_id: "fixture-project",
      session_metadata_key: "session",
      sandbox_namespace_metadata_key: "namespace",
      sandbox_service_account_metadata_key: "account",
    },
  });
  await expect(listSandboxPresets()).resolves.toEqual([
    {
      name: "work",
      title: "Work",
      template: "linux",
      policy_sets: ["baseline"],
      inbox_tags: [],
      session_defaults: {
        harness: "HARNESS_CODEX",
        model: "model-a",
        cwd: "/workspace",
        reasoning_effort: "medium",
        instructions: "",
        setup_script: "echo ready",
      },
      bootstrap: "mkdir -p /workspace",
    },
  ]);
  expect(paths).toEqual([
    "/upstream/app-config/v1/models",
    "/upstream/app-config/v1/frontend-config",
    "/upstream/app-config/v1/presets",
  ]);
});

it("maps omitted ProtoJSON message fields to absent optional links", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({})));
  await expect(getFrontendConfig()).resolves.toEqual({ trace_search: null, langfuse_search: null });
});
