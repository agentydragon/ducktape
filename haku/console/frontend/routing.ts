import { useCallback, useEffect, useState } from "react";

// The console's views are distinguished by URL *path*. `/tool-calls` is the console's own
// full-page history view; **every other path belongs to the framed haku-ui route** — the
// shell mirrors the iframe's route straight into the console pathname
// (haku_ui_embed.tsx's routeChanged handler), so `haku.allegedly.works/garden/<file>`
// deep-links restore both the shell and the frame (operator, 2026-07-13 — path-form
// links used to 404 / only hash-form worked). The hash is no longer load-bearing; legacy
// `#/…` console URLs still restore via the embed's hash fallback. (Operator settings is
// not a view — it's a shell-chrome panel; see shell_chrome.tsx's ShellChrome.)
//
//   "/tool-calls"     → the console's own full-page history of every past MCP tool call
//   anything else     → the full-page haku-ui embed, at that mirrored route
//
// Production's nginx serves index.html for any non-asset/API path (`try_files $uri
// /index.html`), and the dev fallback mirrors that (app.py), so deep-linking any path
// loads the SPA, which then renders the matching view from the pathname.
export const TOOL_CALLS_PATH = "/tool-calls";
export const HOME_PATH = "/";

export type ConsoleView = "embed" | "toolCalls";

// TODO: Recognize `/tool-calls/<id>` as the toolCalls view and pass the id through so
// tool_calls_page.tsx can focus/highlight the canonical call named by an MCP promise URL.
export function viewForPathname(pathname: string): ConsoleView {
  if (pathname === TOOL_CALLS_PATH) return "toolCalls";
  return "embed";
}

// The embed route survives a detour through the console's own views: navigating to
// /tool-calls would otherwise lose the mirrored haku-ui path, and "back to embed" would
// dump the operator on the home view instead of where they were.
let lastEmbedPath: string = HOME_PATH;

export function rememberEmbedPath(path: string): void {
  lastEmbedPath = path;
}

function pathForView(view: ConsoleView): string {
  if (view === "toolCalls") return TOOL_CALLS_PATH;
  return lastEmbedPath;
}

export function useConsoleView(): { view: ConsoleView; navigate: (view: ConsoleView) => void } {
  const [pathname, setPathname] = useState(() => window.location.pathname);
  useEffect(() => {
    const onPop = () => setPathname(window.location.pathname);
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);
  const navigate = useCallback((next: ConsoleView) => {
    if (viewForPathname(window.location.pathname) === "embed") {
      rememberEmbedPath(window.location.pathname);
    }
    const path = pathForView(next);
    if (window.location.pathname === path) return;
    history.pushState(null, "", `${path}${window.location.search}`);
    setPathname(path);
  }, []);
  return { view: viewForPathname(pathname), navigate };
}
