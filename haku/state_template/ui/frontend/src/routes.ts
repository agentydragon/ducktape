// Hash routing — the SPA's own URL is the source of truth for which surface is open
// (plans/garden-gradient.md → "Give the gradient real URLs"). Hash, not path, so the
// StaticFiles backend needs no catch-all and nothing depends on history.pushState
// semantics inside the sandboxed console iframe; `location.hash`/`hashchange` are the
// primitive, safe pair. Hand-rolled on purpose: the route space is small and flat — a
// router dependency would be premature machinery.
//
// Scheme:  #/            Inbox (default; unknown routes also fall back here)
//          #/<view>      any other top-level tab (runs, improvements, …)
//          #/garden      Garden index
//          #/garden/<repo-relative-path>   a specific garden file

import { useCallback, useEffect, useState } from "react";

export type View = "inbox" | "improvements" | "runs" | "garden";

export interface Route {
  view: View;
  /** Only meaningful for view === "garden": the open file, null = the index. */
  gardenPath: string | null;
}

const VIEWS: readonly View[] = ["inbox", "improvements", "runs", "garden"];

export const HOME: Route = { view: "inbox", gardenPath: null };

export function parseHash(hash: string): Route {
  const path = hash.replace(/^#\/?/, "");
  if (!path) return HOME;
  const [head, ...rest] = path.split("/");
  if (head === "garden") {
    const segments = rest.filter((s) => s.length > 0); // "#/garden/" or "//" collapse to the index
    if (segments.length === 0) return { view: "garden", gardenPath: null };
    try {
      return { view: "garden", gardenPath: segments.map(decodeURIComponent).join("/") };
    } catch {
      // Malformed percent-encoding in a user-edited URL — same contract as any other
      // unparseable route: fall back, never throw (this runs in the useState initializer).
      return HOME;
    }
  }
  if ((VIEWS as readonly string[]).includes(head)) {
    return { view: head as View, gardenPath: null };
  }
  return HOME;
}

export function formatHash(route: Route): string {
  if (route.view === "garden" && route.gardenPath) {
    // Encode per segment so slashes stay readable in the address bar.
    return "#/garden/" + route.gardenPath.split("/").map(encodeURIComponent).join("/");
  }
  return route.view === "inbox" ? "#/" : `#/${route.view}`;
}

/** The URL hash as React state: parse on mount, follow `hashchange`, navigate by writing
 * `location.hash` (so the browser owns history/back/forward — F5 and permalinks come free). */
export function useHashRoute(): [Route, (next: Route) => void] {
  const [route, setRoute] = useState<Route>(() => parseHash(window.location.hash));
  useEffect(() => {
    const onHashChange = () => setRoute(parseHash(window.location.hash));
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);
  const navigate = useCallback((next: Route) => {
    const target = formatHash(next);
    if (window.location.hash === target) setRoute(next);
    else window.location.hash = target; // the hashchange listener updates state
  }, []);
  return [route, navigate];
}
