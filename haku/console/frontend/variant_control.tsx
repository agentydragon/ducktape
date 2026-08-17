import { SegmentedControl } from "@mantine/core";
import { useState } from "react";

import { ListDetailsIcon, ListIcon } from "./icons";
import type { PreviewVariant } from "./tool_rendering/vocabulary";

// The per-view compact/detailed selection, shared by the history page's per-row control and the
// approvals panel's cards. The owner holds the state, threading `variant` down (to
// ToolArgumentsField and to gate the detail-only fields) and `setVariant` up to the control.
export function useVariant(initial: PreviewVariant): [PreviewVariant, (v: PreviewVariant) => void] {
  return useState(initial);
}

/** Brief↔Full detail selector: a plain list (compact) vs a list-with-detail-lines (detailed). It
 * belongs at the **top** of a card, because switching to Full grows content below it and the
 * control has to stay put under the pointer. Both states show at once, unlike a lone "Show details"
 * caret, and each segment keeps a `title`/`aria-label` so the icons aren't ambiguous. */
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
