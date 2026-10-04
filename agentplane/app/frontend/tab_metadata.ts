import type { ThreadStatusKind } from "./thread_status_palette";

export const DEFAULT_APP_TITLE = "Agentplane";

export interface ThreadTabStatus {
  kind: ThreadStatusKind;
  label: string;
  tabLabel: string;
}

/** The shell's non-thread routes, named so their browser tabs identify the current surface. */
export function appDocumentTitle(pathname: string, settingsOpen: boolean): string {
  if (settingsOpen) return `Settings — ${DEFAULT_APP_TITLE}`;
  if (pathname === "/actions" || pathname.startsWith("/actions/")) return `Actions — ${DEFAULT_APP_TITLE}`;
  if (pathname === "/sandboxes") return `Sandboxes — ${DEFAULT_APP_TITLE}`;
  if (pathname.startsWith("/sandboxes/")) {
    const encodedName = pathname.slice("/sandboxes/".length).split("/")[0];
    let name = encodedName;
    try {
      name = decodeURIComponent(encodedName);
    } catch {
      // Keep the encoded route segment as a legible fallback for a malformed URL.
    }
    return `${name} — ${DEFAULT_APP_TITLE}`;
  }
  if (pathname.startsWith("/connection-enrollments/")) return `Connect account — ${DEFAULT_APP_TITLE}`;
  return DEFAULT_APP_TITLE;
}

export function threadDocumentTitle(name: string | null | undefined, threadId: string, status: string): string {
  const identity = name?.trim() || `Thread ${threadId.slice(0, 8)}`;
  return `${identity} · ${status} — ${DEFAULT_APP_TITLE}`;
}
