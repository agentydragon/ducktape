import { Switch } from "@mantine/core";
import type { JSX } from "react";

/** The switch between a view's pretty rendering and its recorded contents, raw; off is pretty. */
export function RawSwitch({
  raw,
  onChange,
  ariaLabel,
}: {
  raw: boolean;
  onChange: (raw: boolean) => void;
  ariaLabel?: string;
}): JSX.Element {
  return (
    <Switch
      label="Raw"
      aria-label={ariaLabel}
      checked={raw}
      onChange={(event) => onChange(event.currentTarget.checked)}
    />
  );
}
