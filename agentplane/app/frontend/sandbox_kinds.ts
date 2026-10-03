import type { SandboxCapability, SandboxKind } from "./client";

export const SANDBOX_KIND_OPTIONS: { value: SandboxKind; label: string }[] = [
  { value: "agent_sandbox", label: "Agent Sandbox" },
  { value: "kubevirt", label: "KubeVirt VM" },
];

export const CAPABILITY_LABELS: Record<SandboxCapability, string> = {
  pod_exec: "Pod exec",
  stop_start: "Stop / start",
  live_migration: "Live migration",
  ram_suspend: "RAM suspend",
};

export function sandboxKindLabel(kind: SandboxKind | undefined): string {
  return SANDBOX_KIND_OPTIONS.find((option) => option.value === kind)?.label ?? "Agent Sandbox";
}

export function sandboxRoute(name: string, kind: SandboxKind): string {
  const path = `/sandboxes/${encodeURIComponent(name)}`;
  return kind === "agent_sandbox" ? path : `${path}?kind=${encodeURIComponent(kind)}`;
}
