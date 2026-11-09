import type { ListModelsResponse, ModelOption } from "../app_service_pb";
import { Harness } from "../../proto/session_pb";

/** An empty model list disables new-session selection, not the native harness. */
export function harnessOptions(
  catalog: ListModelsResponse | null
): { value: string; label: string; disabled: boolean }[] {
  const harnesses = [Harness.CLAUDE, Harness.CODEX];
  if (catalog?.harnesses.some((offer) => offer.harness === Harness.CENTRAL_SAMPLER))
    harnesses.push(Harness.CENTRAL_SAMPLER);
  return harnesses.map((harness) => {
    const label = harnessLabel(harness);
    const disabled = !catalog?.harnesses.find((offer) => offer.harness === harness)?.models.length;
    return { value: String(harness), label: disabled && catalog ? `${label} (no models offered)` : label, disabled };
  });
}

/** A harness as pages name it; absent metadata or an unknown enum has no display name. */
export function harnessLabel(harness: Harness | undefined): string {
  switch (harness) {
    case Harness.CLAUDE:
      return "Claude";
    case Harness.CODEX:
      return "Codex";
    case Harness.CENTRAL_SAMPLER:
      return "Central sampler";
    case Harness.UNSPECIFIED:
    case undefined:
    default:
      return "Unknown harness";
  }
}

/** The models offered for one harness, resolved from the catalog's deduplicated `models`
 * list via its `harnesses` id references. */
export function modelsForHarness(catalog: ListModelsResponse, harness: Harness): ModelOption[] {
  const byId = new Map(catalog.models.map((option) => [option.model, option]));
  return (catalog.harnesses.find((offer) => offer.harness === harness)?.models ?? []).flatMap((id) => {
    const option = byId.get(id);
    return option ? [option] : [];
  });
}
