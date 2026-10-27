import { createContext, type JSX, type ReactNode, useContext } from "react";

import type { components } from "../api/schema";
import { useLive, type Live, type SnapshotSchemas } from "../live";
import { zSandboxSnapshot, zSandboxesSnapshot, zWatchHealth } from "../api/live_schema.zod";

export type SandboxesSnapshot = components["schemas"]["SandboxesSnapshot"];
export type SandboxSnapshot = components["schemas"]["SandboxSnapshot"];
export type DecisionsSnapshot = components["schemas"]["DecisionsSnapshot"];

export const sandboxLiveSchemas: SnapshotSchemas<SandboxSnapshot> = {
  snapshot: zSandboxSnapshot,
  health: zWatchHealth,
};
export const sandboxesLiveSchemas: SnapshotSchemas<SandboxesSnapshot> = {
  snapshot: zSandboxesSnapshot,
  health: zWatchHealth,
};

export function liveSandboxesUrl(): string {
  return "/live/sandboxes";
}

export function liveSandboxUrl(name: string, includeArchived: boolean): string {
  return `/live/sandboxes/${encodeURIComponent(name)}?include_archived=${includeArchived}`;
}

export function liveEgressDecisionsUrl(name: string): string {
  return `/live/sandboxes/${encodeURIComponent(name)}/egress/decisions`;
}

const SandboxesLiveContext = createContext<Live<SandboxesSnapshot> | null>(null);

/** One /live/sandboxes connection for thread routes that need current runner availability. */
export function SandboxesLiveProvider({ children }: { children: ReactNode }): JSX.Element {
  const live = useLive<SandboxesSnapshot>(liveSandboxesUrl(), "Sandboxes", sandboxesLiveSchemas);
  return <SandboxesLiveContext.Provider value={live}>{children}</SandboxesLiveContext.Provider>;
}

export function useRequiredSandboxesLive(): Live<SandboxesSnapshot> {
  const live = useContext(SandboxesLiveContext);
  if (!live) throw new Error("Sandbox status requires SandboxesLiveProvider");
  return live;
}
