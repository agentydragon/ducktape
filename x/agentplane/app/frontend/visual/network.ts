/**
 * The harness's stand-in for the network, installed at import time. `openapi-fetch` captures
 * `globalThis.fetch` when `client.ts` creates its client, which happens as the app is imported, so
 * this module must be imported before the app: the harness lists it first. Routes are registered
 * afterwards, since a request cannot arrive before the app has mounted. The ledger is what
 * visual-test-lib's `assertNetworkSettled` reads.
 */

export type Route = [
  method: string,
  pattern: RegExp,
  answer: (match: RegExpMatchArray, query: URLSearchParams) => unknown,
];

export const routes: Route[] = [];

interface Ledger {
  pending: string[];
  violations: string[];
}

const ledger: Ledger = { pending: [], violations: [] };
(window as unknown as { __visualNetworkLedger__: Ledger }).__visualNetworkLedger__ = ledger;

window.fetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
  const url = new URL(
    typeof input === "string" ? input : input instanceof Request ? input.url : input.href,
    "http://harness"
  );
  const method = (init?.method ?? (input instanceof Request ? input.method : "GET")).toUpperCase();
  const key = `${method} ${url.pathname}${url.search}`;
  ledger.pending.push(key);
  try {
    for (const [routeMethod, pattern, answer] of routes) {
      const match = url.pathname.match(pattern);
      if (routeMethod !== method || !match) continue;
      const body = answer(match, url.searchParams);
      if (body === undefined) return Response.json({ detail: `no such sandbox ${match[1]}` }, { status: 404 });
      return Response.json(body);
    }
    ledger.violations.push(`unmatched ${key}`);
    return Response.json({ detail: "not in the harness" }, { status: 503 });
  } finally {
    ledger.pending.splice(ledger.pending.indexOf(key), 1);
  }
};
