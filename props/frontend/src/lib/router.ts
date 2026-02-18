// Hash-based router for Svelte 5.
// Uses the URL constructor for proper parsing — no manual string splitting.
import { writable, derived } from "svelte/store";

function parseHash(hash: string): { pathname: string; searchParams: URLSearchParams } {
  // Parse hash fragment (e.g. "#/runs?definition=sha256:...") as a URL.
  const fragment = hash.slice(1) || "/";
  const url = new URL(fragment, "http://x");
  return { pathname: url.pathname, searchParams: url.searchParams };
}

function createRouter() {
  const hash = writable(window.location.hash);

  if (typeof window !== "undefined") {
    window.addEventListener("hashchange", () => {
      hash.set(window.location.hash);
    });
  }

  return {
    hash: { subscribe: hash.subscribe },
    navigate(to: string) {
      window.location.hash = to;
    },
  };
}

const router = createRouter();
const parsed = derived(router.hash, parseHash);

// Clean pathname (no query string). Use for route matching and nav highlighting.
export const pathname = derived(parsed, ($p) => $p.pathname);

// Current query params as URLSearchParams.
export const searchParams = derived(parsed, ($p) => $p.searchParams);

export function goto(path: string) {
  router.navigate(path);
}

// Resolve a path to a full href (prefixes with #).
export function resolve(path: string): string {
  return "#" + path;
}

// Parse SvelteKit-style route params from a path pattern.
// e.g. parseParams("/runs/[runId]", "/runs/abc") → { runId: "abc" }
export function parseParams(pattern: string, path: string): Record<string, string> | null {
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

  const match = path.match(new RegExp("^" + regexStr + "$"));
  if (!match) return null;

  const params: Record<string, string> = {};
  paramNames.forEach((name, i) => {
    params[name] = match[i + 1];
  });
  return params;
}
