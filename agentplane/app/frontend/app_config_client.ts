import { fromJson, type JsonValue as ProtoJsonValue } from "@bufbuild/protobuf";

import { fetchWithLogin, HttpError } from "./client";
import {
  ListModelsResponseSchema,
  ListSandboxPresetsResponseSchema,
  type ListModelsResponse,
  type ListSandboxPresetsResponse,
} from "../config_service_pb";
import { Harness as ProtoHarness } from "../../proto/runner_pb";

export type Harness = "HARNESS_CLAUDE" | "HARNESS_CODEX" | "HARNESS_CENTRAL_SAMPLER";

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

function isJsonValue(value: unknown): value is ProtoJsonValue {
  if (value === null || typeof value === "string" || typeof value === "boolean") return true;
  if (typeof value === "number") return Number.isFinite(value);
  if (Array.isArray(value)) return value.every(isJsonValue);
  if (typeof value !== "object") return false;
  return Object.values(value).every(isJsonValue);
}

async function call(path: string): Promise<ProtoJsonValue> {
  const response = await fetchWithLogin(`${APP_CONFIG_PATH}${path}`, { headers: { Accept: "application/json" } });
  const text = await response.text();
  let payload: unknown;
  try {
    payload = JSON.parse(text);
  } catch {
    payload = text;
  }
  if (!response.ok) throw new HttpError(response, payload);
  if (!isJsonValue(payload)) throw new Error("Invalid app configuration response.");
  return payload;
}

function harnessName(harness: ProtoHarness): Harness {
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
  const response: ListModelsResponse = fromJson(ListModelsResponseSchema, await call("/models"));
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
  const response: ListSandboxPresetsResponse = fromJson(ListSandboxPresetsResponseSchema, await call("/presets"));
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
