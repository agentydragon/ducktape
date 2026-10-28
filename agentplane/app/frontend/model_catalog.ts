import type { Harness, ModelCatalog, ModelOption } from "./api/schema";
import { api, ok } from "./client";

/** An empty model list disables new-session selection, not the native harness. */
export function harnessOptions(catalog: ModelCatalog | null): { value: Harness; label: string; disabled: boolean }[] {
  const harnesses: { value: Harness; label: string }[] = [
    { value: "HARNESS_CLAUDE", label: "Claude" },
    { value: "HARNESS_CODEX", label: "Codex" },
  ];
  return harnesses.map(({ value, label }) => {
    const disabled = !catalog?.harnesses[value]?.length;
    return { value, label: disabled && catalog ? `${label} (no models offered)` : label, disabled };
  });
}

/** A harness as the pages name it; null while a Thread has no runner attachment to say. */
export function harnessLabel(harness: Harness | null): string {
  switch (harness) {
    case "HARNESS_CLAUDE":
      return "Claude";
    case "HARNESS_CODEX":
      return "Codex";
    case null:
      return "Unknown harness";
  }
}

/** The models offered for one harness, resolved from the catalog's deduplicated `models`
 * list via its `harnesses` id references. */
export function modelsForHarness(catalog: ModelCatalog, harness: Harness): ModelOption[] {
  const byId = new Map(catalog.models.map((option) => [option.model, option]));
  return (catalog.harnesses[harness] ?? []).flatMap((id) => {
    const option = byId.get(id);
    return option ? [option] : [];
  });
}
export async function models(): Promise<ModelCatalog> {
  return ok(await api.GET("/models"));
}
