import { THREAD_STATUS_MARKS, type ThreadStatusKind } from "./status_mark";

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

/** What a thread's tab title needs of its status: the kind picks the leading glyph, the label is the text. */
export type ThreadTabTitleStatus = Pick<ThreadTabStatus, "kind" | "tabLabel">;

export const CONNECTING_TAB_STATUS: ThreadTabTitleStatus = { kind: "inactive", tabLabel: "Connecting" };

/** The glyph leads because a narrow tab truncates the end of a title, and the status label with it. */
export function threadDocumentTitle(
  name: string | null | undefined,
  threadId: string,
  status: ThreadTabTitleStatus
): string {
  const identity = name?.trim() || `Thread ${threadId.slice(0, 8)}`;
  return `${THREAD_STATUS_MARKS[status.kind].glyph} ${identity} · ${status.tabLabel} — ${DEFAULT_APP_TITLE}`;
}
