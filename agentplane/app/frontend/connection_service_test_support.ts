import { vi } from "vitest";

export interface Sent {
  method: string;
  path: string;
  query: URLSearchParams;
  body: unknown;
}

/** Answers ConnectionService facade requests in place of the browser network. */
export function answerConnectionFacade(answer: (sent: Sent, request: Request) => unknown): Sent[] {
  const sent: Sent[] = [];
  vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const target = input instanceof Request ? input.url : input instanceof URL ? input.href : input;
    const request =
      input instanceof Request ? new Request(input, init) : new Request(new URL(target, "https://app.invalid"), init);
    const url = new URL(request.url, "https://app.invalid");
    const text = await request.text();
    const entry: Sent = {
      method: request.method,
      path: url.pathname,
      query: url.searchParams,
      body: text ? JSON.parse(text) : undefined,
    };
    sent.push(entry);
    const response = await answer(entry, request);
    if (response === undefined) throw new Error(`unexpected ${entry.method} ${entry.path}`);
    return response instanceof Response ? response : Response.json(response);
  });
  return sent;
}
