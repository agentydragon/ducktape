import { useSyncExternalStore } from "react";

export interface RouteLocation {
  pathname: string;
  searchParams: URLSearchParams;
}

export function parseHash(hash: string): RouteLocation {
  // Parse the hash fragment (e.g. "#/runs?status=exited") as a URL.
  const fragment = hash.slice(1) || "/";
  const url = new URL(fragment, "http://x");
  return { pathname: url.pathname, searchParams: url.searchParams };
}

let location = parseHash(typeof window === "undefined" ? "" : window.location.hash);
const listeners = new Set<() => void>();

function updateLocation(): void {
  const next = parseHash(window.location.hash);
  if (next.pathname === location.pathname && next.searchParams.toString() === location.searchParams.toString()) return;
  location = next;
  for (const listener of listeners) listener();
}

if (typeof window !== "undefined") window.addEventListener("hashchange", updateLocation);

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function getLocation(): RouteLocation {
  return location;
}

export function useRoute(): RouteLocation {
  return useSyncExternalStore(subscribe, getLocation, getLocation);
}

export function usePathname(): string {
  return useRoute().pathname;
}

export function useSearchParams(): URLSearchParams {
  return useRoute().searchParams;
}

export function goto(path: string): void {
  window.location.hash = path;
}

export function resolve(path: string): string {
  return `#${path}`;
}

/** Parse route params from a path pattern, such as `/runs/[runId]`. */
export function parseParams(pattern: string, path: string): Record<string, string> | null {
  const paramNames: string[] = [];
  const regexStr = pattern
    .replace(/\[\.\.\.(\w+)\]/g, (_, name: string) => {
      paramNames.push(name);
      return "(.+)";
    })
    .replace(/\[(\w+)\]/g, (_, name: string) => {
      paramNames.push(name);
      return "([^/]+)";
    });

  const match = path.match(new RegExp(`^${regexStr}$`));
  if (!match) return null;

  return Object.fromEntries(paramNames.map((name, index) => [name, match[index + 1]]));
}
