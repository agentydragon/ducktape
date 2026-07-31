import { SegmentedControl } from "@mantine/core";
import { useState } from "react";

import { ListDetailsIcon, ListIcon } from "./icons";
import type { PreviewVariant } from "./tool_rendering/vocabulary";

// The per-view compact/detailed selection, shared by the history page's per-row control and the
// approvals panel's cards so both spell it one way. `useVariant` owns the state; the owner threads the
// resulting `variant` down (to ToolArgumentsField and to gate the detail-only fields) and the
// `setVariant` up to the control, so the choice propagates rather than being hardcoded per leaf.
export function useVariant(initial: PreviewVariant): [PreviewVariant, (v: PreviewVariant) => void] {
  return useState(initial);
}

/** Brief↔Full detail selector, rendered as a two-segment flip-flop of icons: a plain list
 * (compact) vs a list-with-detail-lines (detailed). It's placed at the **top** of a card (not the
 * bottom) on purpose: switching to Full grows content below it, so the control stays put under the
 * pointer — you never have to chase it down the card to switch back. A segmented control also
 * shows both states at once, so it's self-explanatory (unlike a lone "Show details" caret). Each
 * segment keeps a `title`/`aria-label` so the icons aren't ambiguous. */
export function VariantControl({
  variant,
  onChange,
}: {
  variant: PreviewVariant;
  onChange: (v: PreviewVariant) => void;
}) {
  return (
    <SegmentedControl
      size="xs"
      value={variant}
      onChange={(v) => onChange(v as PreviewVariant)}
      data={[
        {
          value: "compact",
          label: (
            <span title="Brief" aria-label="Brief" style={{ display: "flex" }}>
              <ListIcon size={16} />
            </span>
          ),
        },
        {
          value: "detailed",
          label: (
            <span title="Full" aria-label="Full" style={{ display: "flex" }}>
              <ListDetailsIcon size={16} />
            </span>
          ),
        },
      ]}
      aria-label="Detail level"
    />
  );
}
