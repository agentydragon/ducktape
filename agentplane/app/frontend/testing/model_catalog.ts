import { create, toJson, type JsonValue, type MessageInitShape } from "@bufbuild/protobuf";

import {
  ListModelsResponseSchema,
  ListSandboxPresetsResponseSchema,
  type ListModelsResponse,
  type ModelOptionSchema,
  type SandboxPreset,
} from "../../app_service_pb";
import { Harness } from "../../../proto/session_pb";

/** Shared model metadata for UI tests that are not specifically testing effort availability. */
export const TEST_REASONING_EFFORTS: string[] = ["low", "medium", "high"];

type ModelOptionInit = MessageInitShape<typeof ModelOptionSchema>;

/** Build the API shape from each harness's offerings, including an empty (disabled) list. */
export function testModelCatalog(claude: ModelOptionInit[], codex: ModelOptionInit[]): ListModelsResponse {
  return create(ListModelsResponseSchema, {
    models: [...claude, ...codex],
    harnesses: [
      { harness: Harness.CLAUDE, models: claude.map((option) => option.model ?? "") },
      { harness: Harness.CODEX, models: codex.map((option) => option.model ?? "") },
    ],
  });
}

/** Serialize the UI test catalog as the app-config gateway's ProtoJSON response. */
export function testModelCatalogProtoJSON(catalog: ListModelsResponse): JsonValue {
  return toJson(ListModelsResponseSchema, catalog);
}

/** Serialize UI preset fixtures as the app-config gateway's ProtoJSON response. */
export function testSandboxPresetsProtoJSON(presets: SandboxPreset[]): JsonValue {
  return toJson(ListSandboxPresetsResponseSchema, create(ListSandboxPresetsResponseSchema, { presets }));
}
