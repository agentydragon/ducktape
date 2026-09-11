// @vitest-environment happy-dom
import { MantineProvider } from "@mantine/core";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";
import { PushSettings } from "./push";

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
        <MantineProvider>
          <PushSettings />
        </MantineProvider>
      )
    );
    expect(container.textContent).toContain("Test browser");
    expect(container.textContent).toContain("(this browser)");
    expect(container.textContent).toContain("Browser notifications are not configured on this server");
    expect(
      [...container.querySelectorAll("button")].find((button) => button.textContent === "Register this browser")
        ?.disabled
    ).toBe(true);
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
        <MantineProvider>
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
