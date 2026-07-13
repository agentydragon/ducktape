import { useCallback, useEffect, useState } from "react";

// The console has two top-level views, distinguished by URL *path*. The path is used —
// never the hash — because the hash is already reserved for mirroring the framed
// haku-ui route (haku_ui_embed.tsx's routeChanged handler), so a path keeps the console's
// own navigation from colliding with the frame's. (Operator settings is no longer a
// separate view — it's a shell-chrome panel; see shell_chrome.tsx's ShellChrome.)
//
//   "/"            → the full-page haku-ui embed (the trusted shell)
//   "/tool-calls"  → the console's own full-page history of every past MCP tool call
//
// Production's nginx serves index.html for any non-asset/API path (`try_files $uri
// /index.html`), and the dev fallback mirrors that (app.py), so deep-linking any path
// loads the SPA, which then renders the matching view from the pathname.
export const TOOL_CALLS_PATH = "/tool-calls";
export const HOME_PATH = "/";

export type ConsoleView = "embed" | "toolCalls";

export function viewForPathname(pathname: string): ConsoleView {
  if (pathname === TOOL_CALLS_PATH) return "toolCalls";
  return "embed";
}

function pathForView(view: ConsoleView): string {
  if (view === "toolCalls") return TOOL_CALLS_PATH;
  return HOME_PATH;
}

export function useConsoleView(): { view: ConsoleView; navigate: (view: ConsoleView) => void } {
  const [pathname, setPathname] = useState(() => window.location.pathname);
  useEffect(() => {
    const onPop = () => setPathname(window.location.pathname);
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);
  const navigate = useCallback((next: ConsoleView) => {
    const path = pathForView(next);
    if (window.location.pathname === path) return;
    // Preserve the hash (the mirrored iframe route) and any query across navigation so
    // returning to the embed restores the frame's last view.
    history.pushState(null, "", `${path}${window.location.search}${window.location.hash}`);
    setPathname(path);
  }, []);
  return { view: viewForPathname(pathname), navigate };
}
