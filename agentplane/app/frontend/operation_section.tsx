import type { JSX, ReactNode } from "react";

import type { CodeBlockSurface } from "../../../util/frontend/code_block";

import "../../../util/frontend/code_block.css";
import "./operation_section.css";

/** One operation payload surface, with its border and inset chosen by the code-surface policy. */
export function OperationSection({
  children,
  surface = "framed",
}: {
  children: ReactNode;
  surface?: CodeBlockSurface;
}): JSX.Element {
  return <div className={`agentplane-operation-section ui-code-surface-${surface}`}>{children}</div>;
}
