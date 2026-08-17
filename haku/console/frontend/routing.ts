import { useCallback, useEffect, useState } from "react";

// The trusted console owns one reserved namespace. Every other pathname belongs to the
// cross-origin Haku UI frame.
export const CONSOLE_ROOT_PATH = "/_console";
export const SETTINGS_PATH = `${CONSOLE_ROOT_PATH}/settings`;
export const TOOL_CALLS_PATH = `${CONSOLE_ROOT_PATH}/tool-calls`;
export const CONVERSATIONS_PATH = `${CONSOLE_ROOT_PATH}/conversations`;
export const OAUTH_RESULT_PATH_PREFIX = `${CONSOLE_ROOT_PATH}/oauth-result`;
export const AGENT_ENROLLMENT_PATH_PREFIX = `${SETTINGS_PATH}/agents/enroll`;
export const HOME_PATH = "/";
const LAST_EMBED_PATH_KEY = "haku-console:last-embed-path";

export type ConsoleNavigationView = "embed" | "settings" | "toolCalls" | "conversations";
export type ConsoleView = ConsoleNavigationView | "agentEnrollment" | "oauthResult" | "sessionFrames" | "notFound";

// Every id-bearing console route carries a canonical UUIDv4.
const UUID = "[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}";

// A single call, deep-linked — what a push notification's "Details" opens, and what the MCP server
// advertises to an agent whose call is waiting. It resolves to the ordinary embed view with the
// approvals drawer opened on that call rather than to the history page, since a pending call is
// decided in the drawer. Tool call ids are `tc_` + 24 hex (mcp_approval.py), not UUIDs like the
// other id-bearing routes here.
const TOOL_CALL_PATH = new RegExp(`^${TOOL_CALLS_PATH}/(tc_[0-9a-f]{24})$`, "i");

const OAUTH_RESULT_PATH = new RegExp(`^${OAUTH_RESULT_PATH_PREFIX}/(${UUID})$`, "i");
const AGENT_ENROLLMENT_PATH = new RegExp(`^${AGENT_ENROLLMENT_PATH_PREFIX}/(${UUID})$`, "i");
const CONVERSATION_PATH = new RegExp(`^${CONVERSATIONS_PATH}/(${UUID})$`, "i");
// The raw frame log of one session. Under `/sessions/` rather than under its conversation, because
// a conversation outlives its sessions and has several: the frames belong to exactly one of them.
// Deep-linkable so "look at frame 412" can be a link rather than a set of directions.
const SESSION_FRAMES_PATH = new RegExp(`^${CONSOLE_ROOT_PATH}/sessions/(${UUID})/frames$`, "i");

export function conversationPath(conversationId: string): string {
  return `${CONVERSATIONS_PATH}/${conversationId}`;
}

export function sessionFramesPath(sessionId: string): string {
  return `${CONSOLE_ROOT_PATH}/sessions/${sessionId}/frames`;
}

/** Move the console to one of its own paths. The shell reads the view off `window.location` and
 * listens for `popstate`, which `pushState` does not fire by itself. */
export function navigateToConsolePath(path: string): void {
  history.pushState(null, "", path);
  window.dispatchEvent(new PopStateEvent("popstate"));
}

export function toolCallIdForPathname(pathname: string): string | null {
  return TOOL_CALL_PATH.exec(pathname)?.[1] ?? null;
}

export function oauthResultIdForPathname(pathname: string): string | null {
  return OAUTH_RESULT_PATH.exec(pathname)?.[1] ?? null;
}

export function agentEnrollmentIdForPathname(pathname: string): string | null {
  return AGENT_ENROLLMENT_PATH.exec(pathname)?.[1] ?? null;
}

export function conversationIdForPathname(pathname: string): string | null {
  return CONVERSATION_PATH.exec(pathname)?.[1] ?? null;
}

export function sessionFramesIdForPathname(pathname: string): string | null {
  return SESSION_FRAMES_PATH.exec(pathname)?.[1] ?? null;
}

export function viewForPathname(pathname: string): ConsoleView {
  if (pathname === CONSOLE_ROOT_PATH || pathname === `${CONSOLE_ROOT_PATH}/`) return "embed";
  if (pathname === SETTINGS_PATH) return "settings";
  if (agentEnrollmentIdForPathname(pathname) !== null) return "agentEnrollment";
  if (pathname === TOOL_CALLS_PATH) return "toolCalls";
  if (sessionFramesIdForPathname(pathname) !== null) return "sessionFrames";
  if (pathname === CONVERSATIONS_PATH || conversationIdForPathname(pathname) !== null) return "conversations";
  if (toolCallIdForPathname(pathname) !== null) return "embed";
  if (oauthResultIdForPathname(pathname) !== null) return "oauthResult";
  if (pathname.startsWith(`${CONSOLE_ROOT_PATH}/`)) return "notFound";
  return "embed";
}

function storedEmbedPath(): string {
  let stored: string | null;
  try {
    stored = sessionStorage.getItem(LAST_EMBED_PATH_KEY);
  } catch (error) {
    console.warn("Unable to read the last Haku UI route from session storage", error);
    return HOME_PATH;
  }
  return stored && viewForPathname(stored) === "embed" && !stored.startsWith(CONSOLE_ROOT_PATH) ? stored : HOME_PATH;
}

let lastEmbedPath = storedEmbedPath();

export function rememberedEmbedPath(): string {
  return lastEmbedPath;
}

export function rememberEmbedPath(path: string): void {
  if (viewForPathname(path) !== "embed" || path.startsWith(CONSOLE_ROOT_PATH)) return;
  lastEmbedPath = path;
  try {
    sessionStorage.setItem(LAST_EMBED_PATH_KEY, path);
  } catch (error) {
    console.warn("Unable to remember the last Haku UI route in session storage", error);
  }
}

function pathForView(view: ConsoleNavigationView): string {
  if (view === "settings") return SETTINGS_PATH;
  if (view === "toolCalls") return TOOL_CALLS_PATH;
  if (view === "conversations") return CONVERSATIONS_PATH;
  return rememberedEmbedPath();
}

export function useConsoleView(): {
  view: ConsoleView;
  agentEnrollmentId: string | null;
  oauthResultId: string | null;
  toolCallId: string | null;
  conversationId: string | null;
  sessionFramesId: string | null;
  navigate: (view: ConsoleNavigationView) => void;
} {
  const [pathname, setPathname] = useState(() => window.location.pathname);

  useEffect(() => {
    if (window.location.pathname === CONSOLE_ROOT_PATH || window.location.pathname === `${CONSOLE_ROOT_PATH}/`) {
      const path = rememberedEmbedPath();
      history.replaceState(null, "", `${path}${window.location.search}`);
      setPathname(path);
    }
    const onPop = () => setPathname(window.location.pathname);
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  const navigate = useCallback((next: ConsoleNavigationView) => {
    if (
      viewForPathname(window.location.pathname) === "embed" &&
      !window.location.pathname.startsWith(CONSOLE_ROOT_PATH)
    ) {
      rememberEmbedPath(window.location.pathname);
    }
    const path = pathForView(next);
    if (window.location.pathname === path) return;
    history.pushState(null, "", `${path}${window.location.search}`);
    setPathname(path);
  }, []);

  return {
    view: viewForPathname(pathname),
    agentEnrollmentId: agentEnrollmentIdForPathname(pathname),
    oauthResultId: oauthResultIdForPathname(pathname),
    toolCallId: toolCallIdForPathname(pathname),
    conversationId: conversationIdForPathname(pathname),
    sessionFramesId: sessionFramesIdForPathname(pathname),
    navigate,
  };
}
