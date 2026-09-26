import { Switch } from "@mantine/core";
import type { JSX } from "react";

/** The one switch between a view's pretty rendering and the JSON it holds, raw; off is pretty. */
export function RawSwitch({ raw, onChange }: { raw: boolean; onChange: (raw: boolean) => void }): JSX.Element {
  return <Switch label="Raw" checked={raw} onChange={(event) => onChange(event.currentTarget.checked)} />;
}
