import { Button } from "@mantine/core";
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

/** Flips a view between compact and detailed; the label names the state it switches *to*. */
export function VariantToggle({ variant, onToggle }: { variant: PreviewVariant; onToggle: () => void }) {
  return (
    <Button size="compact-xs" variant="subtle" color="gray" onClick={onToggle}>
      {variant === "compact" ? "Details" : "Compact"}
    </Button>
  );
}
