import { Text } from "@mantine/core";
import type { JSX, ReactNode } from "react";

/** A wrapping line of small text, optionally led by a bold label. */
export function Line({ label, children }: { label?: string; children: ReactNode }): JSX.Element {
  return (
    <Text component="div" size="sm" style={{ overflowWrap: "anywhere", whiteSpace: "pre-wrap" }}>
      {label && (
        <>
          <Text span fw={600} inherit>
            {label}
          </Text>
          {" · "}
        </>
      )}
      {children}
    </Text>
  );
}
