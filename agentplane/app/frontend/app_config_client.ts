import { fromJson } from "@bufbuild/protobuf";

import { createAppServiceClient } from "./app_service_client";
import {
  ListModelsResponseSchema,
  GetFrontendConfigResponseSchema,
  ListSandboxPresetsResponseSchema,
  type GetFrontendConfigResponse as ProtoGetFrontendConfigResponse,
  type ListModelsResponse,
  type ListSandboxPresetsResponse,
} from "../app_service_pb";
import { Harness as ProtoHarness } from "../../proto/session_pb";

export type Harness = "HARNESS_CLAUDE" | "HARNESS_CODEX" | "HARNESS_CENTRAL_SAMPLER";

export type TraceSearchSettings = {
  grafana_base_url: string;
  tempo_datasource_uid: string;
  deployment_environment: string;
};

export type LangfuseSearchSettings = {
  base_url: string;
  project_id: string;
  session_metadata_key: string;
  sandbox_namespace_metadata_key: string;
  sandbox_service_account_metadata_key: string;
};

export type FrontendConfig = {
  trace_search: TraceSearchSettings | null;
  langfuse_search: LangfuseSearchSettings | null;
};

export type ModelOption = {
  model: string;
  display_name: string;
  reasoning_efforts: string[];
};

export type ModelCatalog = {
  models: ModelOption[];
  harnesses: Partial<Record<Harness, string[]>>;
};

export type SessionDefaultsView = {
  harness: Harness;
  model: string;
  cwd: string;
  reasoning_effort: string | null;
  instructions: string;
  setup_script: string;
};

export type SandboxPresetView = {
  name: string;
  title: string;
  template: string;
  policy_sets: string[];
  inbox_tags: string[];
  session_defaults: SessionDefaultsView;
  bootstrap: string;
};

const APP_CONFIG_PATH = "/upstream/app-config/v1";
const appConfigClient = createAppServiceClient(APP_CONFIG_PATH, "Invalid app configuration response.");

export function harnessName(harness: ProtoHarness): Harness {
  switch (harness) {
    case ProtoHarness.CLAUDE:
      return "HARNESS_CLAUDE";
    case ProtoHarness.CODEX:
      return "HARNESS_CODEX";
    case ProtoHarness.CENTRAL_SAMPLER:
      return "HARNESS_CENTRAL_SAMPLER";
    default:
      throw new Error(`App configuration returned an unknown harness (${harness}).`);
  }
}

export async function listModels(): Promise<ModelCatalog> {
  const response: ListModelsResponse = fromJson(ListModelsResponseSchema, await appConfigClient.get("/models"));
  const harnesses: Partial<Record<Harness, string[]>> = {};
  for (const offer of response.harnesses) harnesses[harnessName(offer.harness)] = offer.models;
  return {
    models: response.models.map((model) => ({
      model: model.model,
      display_name: model.displayName,
      reasoning_efforts: model.reasoningEfforts,
    })),
    harnesses,
  };
}

export async function listSandboxPresets(): Promise<SandboxPresetView[]> {
  const response: ListSandboxPresetsResponse = fromJson(
    ListSandboxPresetsResponseSchema,
    await appConfigClient.get("/presets")
  );
  return response.presets.map((preset) => ({
    name: preset.name,
    title: preset.title,
    template: preset.template,
    policy_sets: preset.policySets,
    inbox_tags: preset.inboxTags,
    session_defaults: {
      harness: harnessName(preset.sessionDefaults?.harness ?? 0),
      model: preset.sessionDefaults?.model ?? "",
      cwd: preset.sessionDefaults?.cwd ?? "",
      reasoning_effort: preset.sessionDefaults?.reasoningEffort ?? null,
      instructions: preset.sessionDefaults?.instructions ?? "",
      setup_script: preset.sessionDefaults?.setupScript ?? "",
    },
    bootstrap: preset.bootstrap,
  }));
}

export async function getFrontendConfig(): Promise<FrontendConfig> {
  const response: ProtoGetFrontendConfigResponse = fromJson(
    GetFrontendConfigResponseSchema,
    await appConfigClient.get("/frontend-config")
  );
  const config = response.frontendConfig;
  return {
    trace_search: config?.traceSearch
      ? {
          grafana_base_url: config.traceSearch.grafanaBaseUrl,
          tempo_datasource_uid: config.traceSearch.tempoDatasourceUid,
          deployment_environment: config.traceSearch.deploymentEnvironment,
        }
      : null,
    langfuse_search: config?.langfuseSearch
      ? {
          base_url: config.langfuseSearch.baseUrl,
          project_id: config.langfuseSearch.projectId,
          session_metadata_key: config.langfuseSearch.sessionMetadataKey,
          sandbox_namespace_metadata_key: config.langfuseSearch.sandboxNamespaceMetadataKey,
          sandbox_service_account_metadata_key: config.langfuseSearch.sandboxServiceAccountMetadataKey,
        }
      : null,
  };
}
