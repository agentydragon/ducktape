// Simple hash-based router for Svelte 5
import { writable, derived } from "svelte/store";

// Current hash path (without the #)
function createRouter() {
  const path = writable(window.location.hash.slice(1) || "/");

  // Listen for hash changes
  if (typeof window !== "undefined") {
    window.addEventListener("hashchange", () => {
      path.set(window.location.hash.slice(1) || "/");
    });
  }

  return {
    subscribe: path.subscribe,
    navigate: (to: string) => {
      window.location.hash = to;
    },
  };
}

export const router = createRouter();

// Derived store for current pathname
export const pathname = derived(router, ($r) => $r);

// Navigate programmatically
export function goto(path: string) {
  router.navigate(path);
}

// Resolve paths (replaces $app/paths.resolve)
// With hash routing, base is always empty
export function resolve(path: string): string {
  return "#" + path;
}

// Parse route params from path
export function parseParams(pattern: string, path: string): Record<string, string> | null {
  // Convert pattern like '/runs/:runId' to regex
  const paramNames: string[] = [];
  const regexStr = pattern
    .replace(/\[\.\.\.(\w+)\]/g, (_, name) => {
      paramNames.push(name);
      return "(.+)"; // catch-all
    })
    .replace(/\[(\w+)\]/g, (_, name) => {
      paramNames.push(name);
      return "([^/]+)";
    });

  const regex = new RegExp("^" + regexStr + "$");
  const match = path.match(regex);

  if (!match) return null;

  const params: Record<string, string> = {};
  paramNames.forEach((name, i) => {
    params[name] = match[i + 1];
  });
  return params;
}

// Route matching helper
export function matchRoute(
  routes: Array<{ pattern: string; component: unknown }>,
  path: string
): { component: unknown; params: Record<string, string> } | null {
  for (const route of routes) {
    const params = parseParams(route.pattern, path);
    if (params !== null) {
      return { component: route.component, params };
    }
  }
  return null;
}
