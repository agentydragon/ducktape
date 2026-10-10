import { Code } from "@mantine/core";
import type { JSX, ReactNode } from "react";

/** Unclamped chips: compact may approve the call, so no value may be elided by CSS. */
export function Chip({ label, value }: { label: string; value: ReactNode }): JSX.Element {
  return (
    <Code style={{ whiteSpace: "normal", overflowWrap: "anywhere" }}>
      {label}: {value}
    </Code>
  );
}
