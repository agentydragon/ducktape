import { useState } from "react";

import type { PreviewVariant } from "./tool_previews/variant.tsx";

// The per-view compact/detailed toggle, shared by the history page's per-row control and the
// drawer's recent-call detail so both spell it one way. `useVariant` owns the state and its
// flip; the owner threads the resulting `variant` down (to ToolArgumentsField and to gate the
// detail-only fields), so the choice propagates rather than being hardcoded per leaf.
export function useVariant(initial: PreviewVariant): [PreviewVariant, () => void] {
  const [variant, setVariant] = useState(initial);
  return [variant, () => setVariant((v) => (v === "compact" ? "detailed" : "compact"))];
}

/** Expands/collapses a card's detail, rendered as a caret disclosure that matches the card's
 * other disclosures (Metadata, Raw arguments) — a rotating triangle + "Show details" / "Show
 * less" at the bottom of the card, not a button in the corner. */
export function VariantToggle({ variant, onToggle }: { variant: PreviewVariant; onToggle: () => void }) {
  const detailed = variant === "detailed";
  return (
    <button type="button" className="haku-shell-expander" aria-expanded={detailed} onClick={onToggle}>
      <span className="haku-shell-expander-caret" aria-hidden="true">
        ▸
      </span>
      {detailed ? "Show less" : "Show details"}
    </button>
  );
}
