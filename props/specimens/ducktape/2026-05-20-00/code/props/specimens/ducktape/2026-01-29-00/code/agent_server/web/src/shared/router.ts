import { writable, type Writable } from "svelte/store";

export function getAgentIdFromUrl(): string | null {
  return new URL(window.location.href).searchParams.get("agent_id");
}

export const currentAgentId: Writable<string | null> = writable(
  typeof window !== "undefined" ? getAgentIdFromUrl() : null
);

export function setAgentId(id?: string | null) {
  const url = new URL(window.location.href);
  if (!id) url.searchParams.delete("agent_id");
  else url.searchParams.set("agent_id", id);
  history.pushState({}, "", url.toString());
  window.dispatchEvent(new CustomEvent("agent_id_changed", { detail: { agentId: id || null } }));
  currentAgentId.set(id ?? null);
}

// Sync on navigation events
if (typeof window !== "undefined") {
  const update = () => currentAgentId.set(getAgentIdFromUrl());
  window.addEventListener("popstate", update);
  window.addEventListener("agent_id_changed", update as any);
}
