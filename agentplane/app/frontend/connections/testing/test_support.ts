import { MantineProvider } from "@mantine/core";
import { act, createElement, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, vi } from "vitest";

const mounted: Array<{ root: ReturnType<typeof createRoot>; container: HTMLDivElement }> = [];

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

export interface Sent {
  method: string;
  path: string;
  query: URLSearchParams;
  body: unknown;
}

/** Mounts the ConnectionService UI without installing the Action-only fetch mock. */
export async function mountConnectionService(node: ReactNode): Promise<HTMLDivElement> {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  mounted.push({ root, container });
  await act(async () => root.render(createElement(MantineProvider, { env: "test" }, node)));
  return container;
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

afterEach(async () => {
  for (const item of mounted.splice(0)) {
    await act(async () => item.root.unmount());
    item.container.remove();
  }
  vi.unstubAllGlobals();
});
