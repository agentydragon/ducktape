// Central dispatcher over per-server tool-preview modules (google.tsx,
// kubectl.tsx, ...). Each module owns one server's widgets and returns
// null for anything outside its own serverId, so adding a server is "write a new
// per-server file + add one line here" — this file never grows past that.

import type { ReactNode } from "react";

import { googleToolPreview } from "./google.tsx";
import { grocyToolPreview } from "./grocy.tsx";
import { kubectlToolPreview } from "./kubectl.tsx";

export function toolPreview(serverId: string, toolName: string, args: Record<string, unknown>): ReactNode | null {
  return (
    googleToolPreview(serverId, toolName, args) ??
    kubectlToolPreview(serverId, toolName, args) ??
    grocyToolPreview(serverId, toolName, args)
  );
}
