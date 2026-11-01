import type { ModelCatalog, ModelOption } from "../model_catalog";
import type { SandboxPresetView } from "../model_catalog";

/** Shared model metadata for UI tests that are not specifically testing effort availability. */
export const TEST_REASONING_EFFORTS: string[] = ["low", "medium", "high"];

/** Build the API shape from each harness's offerings, including an empty (disabled) list. */
export function testModelCatalog(claude: ModelOption[], codex: ModelOption[]): ModelCatalog {
  return {
    models: [...claude, ...codex],
    harnesses: {
      HARNESS_CLAUDE: claude.map((option) => option.model),
      HARNESS_CODEX: codex.map((option) => option.model),
    },
  };
}

/** Serialize the UI test catalog as the app-config gateway's ProtoJSON response. */
export function testModelCatalogProtoJSON(catalog: ModelCatalog): object {
  return {
    models: catalog.models.map(({ model, display_name, reasoning_efforts }) => ({
      model,
      displayName: display_name,
      reasoningEfforts: reasoning_efforts,
    })),
    harnesses: Object.entries(catalog.harnesses).map(([harness, models]) => ({ harness, models })),
  };
}

/** Serialize a UI preset fixture as the app-config gateway's ProtoJSON response. */
export function testSandboxPresetsProtoJSON(presets: SandboxPresetView[]): object {
  return {
    presets: presets.map((preset) => ({
      name: preset.name,
      title: preset.title,
      template: preset.template,
      policySets: preset.policy_sets,
      inboxTags: preset.inbox_tags,
      sessionDefaults: {
        harness: preset.session_defaults.harness,
        model: preset.session_defaults.model,
        cwd: preset.session_defaults.cwd,
        ...(preset.session_defaults.reasoning_effort === null
          ? {}
          : { reasoningEffort: preset.session_defaults.reasoning_effort }),
        instructions: preset.session_defaults.instructions,
        setupScript: preset.session_defaults.setup_script,
      },
      bootstrap: preset.bootstrap,
    })),
  };
}
