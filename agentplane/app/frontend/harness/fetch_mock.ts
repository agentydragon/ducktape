/** Browser fetch stub and request ledger shared by the visual harnesses. */

/** An answer is a JSON body, `undefined` for 404, a ready `Response` for any other status, or
 * `UNANSWERED`. */
export type Route = [
  method: string,
  pattern: RegExp,
  answer: (
    match: RegExpMatchArray,
    query: URLSearchParams,
    signal: AbortSignal | undefined,
    body: string | undefined
  ) => unknown,
];

export const routes: Route[] = [];

/**
 * An answer that never comes, as for a request queued behind the browser's connection limit: the
 * fetch stays pending until its signal aborts, and the ledger does not wait for it.
 */
export const UNANSWERED: unique symbol = Symbol("unanswered");

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
  const signal = init?.signal ?? (input instanceof Request ? input.signal : undefined);
  const key = `${method} ${url.pathname}${url.search}`;
  ledger.pending.push(key);
  try {
    for (const [routeMethod, pattern, answer] of routes) {
      const match = url.pathname.match(pattern);
      if (routeMethod !== method || !match) continue;
      const requestBody =
        typeof init?.body === "string"
          ? init.body
          : input instanceof Request && input.body !== null
            ? await input.clone().text()
            : undefined;
      const body = answer(match, url.searchParams, signal, requestBody);
      if (body === UNANSWERED) {
        return new Promise<Response>((_resolve, reject) => {
          if (signal) signal.addEventListener("abort", () => reject(signal.reason), { once: true });
        });
      }
      if (body instanceof Response) return body;
      if (body === undefined) return Response.json({ detail: `no such sandbox ${match[1]}` }, { status: 404 });
      return Response.json(body);
    }
    ledger.violations.push(`unmatched ${key}`);
    return Response.json({ detail: "not in the harness" }, { status: 503 });
  } finally {
    ledger.pending.splice(ledger.pending.indexOf(key), 1);
  }
};
