import { createContext, type JSX, type ReactNode, useContext } from "react";

import { useLive, type Live, type WatchHealth } from "../live";

import type { ActionPolicyUnavailable, ActionPolicyView } from "../actions/client";
import type { SandboxView, ThreadView } from "../schema";
import type { BindingView, Decision } from "./client";

export type SandboxesSnapshot = { sandboxes: SandboxView[]; watch: WatchHealth };
export type SandboxSnapshot = {
  sandbox: SandboxView | null;
  bindings: BindingView[];
  action_policy: ActionPolicyView | ActionPolicyUnavailable | null;
  threads: ThreadView[] | { kind: "unavailable"; code: string };
  updates_connected: boolean;
  watch: WatchHealth;
};
export type DecisionsSnapshot = { watching: boolean; decisions: Decision[] | { detail: string } };

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

/** The app's one /live/sandboxes connection: every view of the Sandbox inventory reads it, so two views of one
 * Sandbox cannot disagree. */
export function SandboxesLiveProvider({ children }: { children: ReactNode }): JSX.Element {
  const live = useLive<SandboxesSnapshot>(liveSandboxesUrl(), "Sandboxes");
  return <SandboxesLiveContext.Provider value={live}>{children}</SandboxesLiveContext.Provider>;
}

/** Whether the inventory stream is current and the server's watch behind it healthy, so its Sandboxes are live. */
export function sandboxInventoryFresh(live: Live<SandboxesSnapshot>): boolean {
  return live.stream.standing === "current" && live.health?.healthy === true;
}

export function useRequiredSandboxesLive(): Live<SandboxesSnapshot> {
  const live = useContext(SandboxesLiveContext);
  if (!live) throw new Error("Sandbox status requires SandboxesLiveProvider");
  return live;
}
