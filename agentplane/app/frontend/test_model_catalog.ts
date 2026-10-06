import type { ModelCatalog, ModelOption } from "./client";

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
