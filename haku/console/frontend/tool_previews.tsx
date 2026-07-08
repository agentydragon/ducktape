// Central dispatcher over per-server tool-preview modules (google_tool_previews.tsx,
// kubectl_tool_previews.tsx, ...). Each module owns one server's widgets and returns
// null for anything outside its own serverId, so adding a server is "write a new
// per-server file + add one line here" — this file never grows past that.

import type { ReactNode } from "react";

import { googleToolPreview } from "./google_tool_previews.tsx";
import { grocyToolPreview } from "./grocy_tool_previews.tsx";
import { kubectlToolPreview } from "./kubectl_tool_previews.tsx";

export function toolPreview(serverId: string, toolName: string, args: Record<string, unknown>): ReactNode | null {
  return (
    googleToolPreview(serverId, toolName, args) ??
    kubectlToolPreview(serverId, toolName, args) ??
    grocyToolPreview(serverId, toolName, args)
  );
}
