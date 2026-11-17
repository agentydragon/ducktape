import type { SessionKind } from "./openapi_schema";

export const SESSION_KINDS: { value: SessionKind; label: string }[] = [
  { value: "main", label: "Main sessions" },
  { value: "subagent", label: "Subagents" },
  { value: "guardian", label: "Guardian" },
  { value: "unknown", label: "Unknown" },
];

export function readSessionViewerSearch(search: string): {
  machineId: string;
  harness: string;
  kindScope: string;
  versionId: string | null;
} {
  const params = new URLSearchParams(search);
  const kindSelection = params.has("kind") ? params.getAll("kind") : ["main", "unknown"];
  const kindScope = SESSION_KINDS.filter(({ value }) => kindSelection.includes(value))
    .map(({ value }) => value)
    .join(",");
  return {
    machineId: params.get("machine") ?? "",
    harness: params.get("harness") ?? "",
    kindScope,
    versionId: params.get("version"),
  };
}

export function sessionKindsFromScope(kindScope: string): SessionKind[] {
  return kindScope === "" ? [] : (kindScope.split(",") as SessionKind[]);
}

export function sessionPath(targetId: string, search: string): string {
  const params = new URLSearchParams(search);
  params.delete("version");
  const query = params.toString();
  return `/sessions/${encodeURIComponent(targetId)}${query ? `?${query}` : ""}`;
}

export function sessionFilterPath(search: string, name: "machine" | "harness", value: string): string {
  const params = new URLSearchParams(search);
  if (value) params.set(name, value);
  else params.delete(name);
  params.delete("version");
  return `/?${params.toString()}`;
}

export function sessionKindsPath(pathname: string, search: string, values: string[]): string {
  const params = new URLSearchParams(search);
  params.delete("kind");
  for (const { value } of SESSION_KINDS) {
    if (values.includes(value)) params.append("kind", value);
  }
  if (values.length === 0) params.set("kind", "");
  return `${pathname}?${params.toString()}`;
}

export function sessionVersionPath(pathname: string, search: string, versionId: string): string {
  const params = new URLSearchParams(search);
  if (versionId) params.set("version", versionId);
  else params.delete("version");
  const query = params.toString();
  return `${pathname}${query ? `?${query}` : ""}`;
}
