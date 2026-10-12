// @vitest-environment happy-dom
import { MantineProvider } from "@mantine/core";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { beforeEach, expect, it, vi } from "vitest";

import { TestEventSource } from "../actions/testing";
import { PushSettings } from "./push";

beforeEach(() => {
  vi.stubGlobal("EventSource", TestEventSource);
});

it("updates registered browsers from the stream without a refresh button", async () => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  const streams: EventTarget[] = [];
  class Source extends EventTarget {
    static readonly CLOSED = 2;
    readyState = 1;
    constructor(readonly url: string) {
      super();
      streams.push(this);
    }
    close() {
      this.readyState = Source.CLOSED;
    }
  }
  vi.stubGlobal("EventSource", Source);
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => Response.json(url === "/push/config" ? { application_server_key: null } : []))
  );
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  try {
    await act(async () =>
      root.render(
        <MantineProvider env="test">
          <PushSettings />
        </MantineProvider>
      )
    );
    expect(container.textContent).not.toContain("Refresh notification settings");
    await act(async () =>
      streams[0]!.dispatchEvent(
        new MessageEvent("snapshot", {
          data: JSON.stringify([
            {
              endpoint: "https://push.example/other",
              user_agent: "Another browser",
              created_at: "2026-01-01T00:00:00Z",
            },
          ]),
        })
      )
    );
    expect(container.textContent).toContain("Another browser");
  } finally {
    await act(async () => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  }
});

it("lists registered browsers, identifies this browser, and unregisters it locally and remotely", async () => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  const endpoint = "https://push.example/current";
  const unsubscribe = vi.fn(async () => true);
  const registration = { pushManager: { getSubscription: async () => ({ endpoint, unsubscribe }) } };
  const previous = Object.getOwnPropertyDescriptor(navigator, "serviceWorker");
  Object.defineProperty(navigator, "serviceWorker", {
    configurable: true,
    value: { getRegistration: async () => registration },
  });
  let registered = true;
  const transport = vi.fn(async (url: string, options?: RequestInit) => {
    if (options?.method === "DELETE") {
      registered = false;
      return new Response(null, { status: 204 });
    }
    return Response.json(
      url === "/push/config"
        ? { application_server_key: null }
        : registered
          ? [{ endpoint, user_agent: "Test browser", created_at: "2026-09-09" }]
          : []
    );
  });
  vi.stubGlobal("fetch", transport);
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  try {
    await act(async () =>
      root.render(
        <MantineProvider env="test">
          <PushSettings />
        </MantineProvider>
      )
    );
    expect(container.textContent).toContain("Test browser");
    expect(container.textContent).toContain("(this browser)");
    expect(container.textContent).toContain("Browser notifications are not configured on this server");
    expect(
      [...container.querySelectorAll("button")].find((button) => button.textContent === "Register this browser")
    ).toBeUndefined();
    const forget = [...container.querySelectorAll("button")].find((button) => button.textContent === "Forget");
    expect(forget).toBeDefined();
    await act(async () => forget?.click());
    expect(unsubscribe).toHaveBeenCalledOnce();
    expect(container.textContent).not.toContain("Test browser");
    expect(transport).toHaveBeenCalledWith(
      `/push/subscriptions?endpoint=${encodeURIComponent(endpoint)}`,
      expect.objectContaining({ method: "DELETE" })
    );
  } finally {
    await act(async () => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
    if (previous) Object.defineProperty(navigator, "serviceWorker", previous);
    else Reflect.deleteProperty(navigator, "serviceWorker");
  }
});

it.each(["Forbidden", ""])("shows failed configuration HTTP status and body (status text: %s)", async (statusText) => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  const body = { detail: { code: "operator_federation_exchange_failed" } };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) =>
      url === "/push/config" ? Response.json(body, { status: 403, statusText }) : Response.json([])
    )
  );
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  try {
    await act(async () =>
      root.render(
        <MantineProvider env="test">
          <PushSettings />
        </MantineProvider>
      )
    );
    expect(container.textContent).toContain(
      `GET /push/config: HTTP 403${statusText ? ` ${statusText}` : ""}: ${JSON.stringify(body)}`
    );
  } finally {
    await act(async () => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  }
});

it("explains pending settings and a failed load without claiming the server is unconfigured", async () => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  let rejectResponse!: (reason: Error) => void;
  const response = new Promise<Response>((_resolve, reject) => {
    rejectResponse = reject;
  });
  vi.stubGlobal(
    "fetch",
    vi.fn(() => response)
  );
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  try {
    await act(async () =>
      root.render(
        <MantineProvider env="test">
          <PushSettings />
        </MantineProvider>
      )
    );
    expect(container.textContent).toContain("Loading notification settings");
    expect(container.textContent).not.toContain("not configured on this server");
    expect(
      [...container.querySelectorAll("button")].find((button) => button.textContent === "Register this browser")
        ?.disabled
    ).toBe(true);
    await act(async () => rejectResponse(new Error("Server unavailable")));
    expect(container.textContent).toContain("Could not load notification settings");
    expect(container.textContent).toContain("Server unavailable");
    expect(container.textContent).not.toContain("Loading notification settings");
    expect(container.textContent).not.toContain("not configured on this server");
  } finally {
    await act(async () => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  }
});
