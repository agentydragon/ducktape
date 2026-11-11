import { SegmentedControl } from "@mantine/core";
import type { JSX } from "react";

import { ListDetailsIcon, ListIcon } from "./icons";
import type { PreviewVariant } from "./tool_rendering/vocabulary";

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
}): JSX.Element {
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
