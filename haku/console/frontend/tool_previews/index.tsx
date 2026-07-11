// Registry mapping each MCP server id to its per-tool preview renderer. Each per-server
// module (google.tsx, grocy.tsx, …) owns one server's widgets and exports its serverId plus
// a (toolName, args, variant) => ReactNode|null renderer; adding a server is "write a new
// module + one registry entry here". `toolPreview` dispatches by serverId, so the file never
// grows a hand-maintained `??` chain. `variant` picks the compact vs detailed rendering.

import type { ReactNode } from "react";

import { GOOGLE_SERVER_ID, googleToolPreview } from "./google.tsx";
import { GROCY_SERVER_ID, grocyToolPreview } from "./grocy.tsx";
import { KUBECTL_SERVER_ID, kubectlToolPreview } from "./kubectl.tsx";
import type { PreviewVariant } from "./variant.tsx";

type ToolPreviewRenderer = (
  toolName: string,
  args: Record<string, unknown>,
  variant: PreviewVariant
) => ReactNode | null;

const RENDERERS: Record<string, ToolPreviewRenderer> = {
  [GOOGLE_SERVER_ID]: googleToolPreview,
  [GROCY_SERVER_ID]: grocyToolPreview,
  [KUBECTL_SERVER_ID]: kubectlToolPreview,
};

export function toolPreview(
  serverId: string,
  toolName: string,
  args: Record<string, unknown>,
  variant: PreviewVariant
): ReactNode | null {
  return RENDERERS[serverId]?.(toolName, args, variant) ?? null;
}
