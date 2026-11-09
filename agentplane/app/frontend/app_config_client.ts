import { create, fromJsonString } from "@bufbuild/protobuf";

import { createAppServiceClient } from "./app_service_client";
import {
  FrontendConfigSchema,
  GetFrontendConfigResponseSchema,
  GetSandboxFormResponseSchema,
  ListModelsResponseSchema,
  ListSandboxPresetsResponseSchema,
  type FrontendConfig,
  type GetSandboxFormResponse,
  type ListModelsResponse,
  type NewSandboxDefaults,
  type SandboxPreset,
} from "../app_service_pb";
import { Harness } from "../../proto/session_pb";

const APP_CONFIG_PATH = "/upstream/app-config/v1";
const appConfigClient = createAppServiceClient(APP_CONFIG_PATH);

function knownHarness(harness: Harness): Harness {
  switch (harness) {
    case Harness.CLAUDE:
    case Harness.CODEX:
    case Harness.CENTRAL_SAMPLER:
      return harness;
    default:
      throw new Error(`App configuration returned an unknown harness (${harness}).`);
  }
}

export async function listModels(): Promise<ListModelsResponse> {
  const response = fromJsonString(ListModelsResponseSchema, await appConfigClient.getText("/models"));
  for (const offer of response.harnesses) knownHarness(offer.harness);
  return response;
}

export async function listSandboxPresets(): Promise<SandboxPreset[]> {
  const response = fromJsonString(ListSandboxPresetsResponseSchema, await appConfigClient.getText("/presets"));
  for (const preset of response.presets) knownHarness(preset.sessionDefaults?.harness ?? Harness.UNSPECIFIED);
  return response.presets;
}

export async function getFrontendConfig(): Promise<FrontendConfig> {
  const response = fromJsonString(GetFrontendConfigResponseSchema, await appConfigClient.getText("/frontend-config"));
  return response.frontendConfig ?? create(FrontendConfigSchema);
}

export async function getSandboxForm(): Promise<GetSandboxFormResponse & { newSandboxDefaults: NewSandboxDefaults }> {
  const response = fromJsonString(GetSandboxFormResponseSchema, await appConfigClient.getText("/sandbox-form"));
  const defaults = response.newSandboxDefaults;
  if (defaults === undefined) throw new Error("App configuration omitted Sandbox defaults.");
  return { ...response, newSandboxDefaults: defaults };
}
