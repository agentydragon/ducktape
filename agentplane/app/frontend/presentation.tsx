import { SegmentedControl } from "@mantine/core";
import type { JSX } from "react";

/** How a card shows what it holds: `pretty`, as the best rendering there is for it, or `raw`, as the
 * JSON it is stored as. */
export type Presentation = "pretty" | "raw";

/** A card's one Pretty/Raw switch. It goes at the top of the card, so it stays under the pointer when
 * switching changes the height of what follows. */
export function PresentationControl({
  value,
  onChange,
}: {
  value: Presentation;
  onChange: (value: Presentation) => void;
}): JSX.Element {
  return (
    <SegmentedControl
      size="xs"
      value={value}
      onChange={(next) => onChange(next as Presentation)}
      data={[
        { value: "pretty", label: "Pretty" },
        { value: "raw", label: "Raw" },
      ]}
      aria-label="Presentation"
    />
  );
}
