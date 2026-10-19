import type { JSX, ReactNode } from "react";

import type { CodeBlockSurface } from "./code_block";

/** One operation payload surface, with its border and inset chosen by the code-surface policy. */
export function OperationSection({
  children,
  surface = "framed",
}: {
  children: ReactNode;
  surface?: CodeBlockSurface;
}): JSX.Element {
  return <div className={`agentplane-operation-section agentplane-code-surface-${surface}`}>{children}</div>;
}
