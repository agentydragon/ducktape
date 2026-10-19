import { execArgumentsPreview, execCollapsedPreview, execLabel, execResultPreview } from "./exec";
import type { ActionPresentation } from "../presentation";

export const sshRenderers: ReadonlyMap<string, ActionPresentation> = new Map<string, ActionPresentation>([
  [
    "exec",
    {
      label: execLabel,
      pane: { collapsed: execCollapsedPreview, opened: execArgumentsPreview },
      details: { arguments: execArgumentsPreview, result: execResultPreview },
    },
  ],
]);
