// @vitest-environment happy-dom
import { MantineProvider } from "@mantine/core";
import { act, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { ServedWatch } from "../testing/watch";
import { PushSettings } from "./push";

const PUSH_PATH = "/upstream/actions/v1/operator/push";
const mounts: { container: HTMLDivElement; root?: Root }[] = [];
let restoreServiceWorker: (() => void) | undefined;

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
});

afterEach(async () => {
  const mounted = mounts.splice(0);
  try {
    for (const { root } of mounted) {
      if (root) await act(async () => root.unmount());
    }
  } finally {
    for (const { container } of mounted) container.remove();
    try {
      vi.unstubAllGlobals();
    } finally {
      restoreServiceWorker?.();
      restoreServiceWorker = undefined;
    }
  }
});

async function mount(element: ReactNode): Promise<{ container: HTMLDivElement; root: Root }> {
  const container = document.createElement("div");
  document.body.append(container);
  const mounted: (typeof mounts)[number] = { container };
  mounts.push(mounted);
  const root = createRoot(container);
  mounted.root = root;
  await act(async () => root.render(element));
  return { container, root };
}

async function mountPushSettings(): Promise<HTMLDivElement> {
  return (
    await mount(
      <MantineProvider env="test">
        <PushSettings />
      </MantineProvider>
    )
  ).container;
}

function watch(url: string, options?: RequestInit, subscriptions: unknown[] = []): ServedWatch {
  const served = new ServedWatch(url, options?.signal ?? undefined);
  served.send({ snapshot: { pushSubscriptions: subscriptions } });
  served.send({ snapshotComplete: {} });
  return served;
}

it("updates registered browsers from the stream without a refresh button", async () => {
  let stream: ServedWatch | undefined;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, options?: RequestInit) => {
      if (url.endsWith(":watch")) {
        stream = watch(url, options);
        return stream.response;
      }
      return Response.json(url === `${PUSH_PATH}/config` ? {} : { pushSubscriptions: [] });
    })
  );
  const container = await mountPushSettings();
  expect(container.textContent).not.toContain("Refresh notification settings");
  await act(async () =>
    stream?.send({
      upsert: {
        endpoint: "https://push.example/other",
        userAgent: "Another browser",
        createdAt: "2026-01-01T00:00:00Z",
      },
    })
  );
  expect(container.textContent).toContain("Another browser");
  await act(async () => stream?.send({ removedEndpoint: "https://push.example/other" }));
  expect(container.textContent).not.toContain("Another browser");
});

it("lists registered browsers, identifies this browser, and unregisters it locally and remotely", async () => {
  const endpoint = "https://push.example/current";
  const unsubscribe = vi.fn(async () => true);
  const registration = { pushManager: { getSubscription: async () => ({ endpoint, unsubscribe }) } };
  const previous = Object.getOwnPropertyDescriptor(navigator, "serviceWorker");
  restoreServiceWorker = () => {
    if (previous) Object.defineProperty(navigator, "serviceWorker", previous);
    else Reflect.deleteProperty(navigator, "serviceWorker");
  };
  Object.defineProperty(navigator, "serviceWorker", {
    configurable: true,
    value: { getRegistration: async () => registration },
  });
  let registered = true;
  const transport = vi.fn(async (url: string, options?: RequestInit) => {
    if (url.endsWith(":watch"))
      return watch(url, options, [{ endpoint, userAgent: "Test browser", createdAt: "2026-09-09T00:00:00Z" }]).response;
    if (options?.method === "DELETE") {
      registered = false;
      return Response.json({});
    }
    return Response.json(
      url === `${PUSH_PATH}/config`
        ? {}
        : {
            pushSubscriptions: registered
              ? [{ endpoint, userAgent: "Test browser", createdAt: "2026-09-09T00:00:00Z" }]
              : [],
          }
    );
  });
  vi.stubGlobal("fetch", transport);
  const container = await mountPushSettings();
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
    `${PUSH_PATH}/subscriptions?endpoint=${encodeURIComponent(endpoint)}`,
    expect.objectContaining({ method: "DELETE" })
  );
});

it.each(["Forbidden", ""])("shows failed configuration HTTP status and body (status text: %s)", async (statusText) => {
  const body = { detail: { code: "operator_federation_exchange_failed" } };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, options?: RequestInit) =>
      url.endsWith(":watch")
        ? watch(url, options).response
        : url === `${PUSH_PATH}/config`
          ? Response.json(body, { status: 403, statusText })
          : Response.json({ pushSubscriptions: [] })
    )
  );
  const container = await mountPushSettings();
  expect(container.textContent).toContain(
    `GET ${PUSH_PATH}/config: HTTP 403${statusText ? ` ${statusText}` : ""}: ${JSON.stringify(body)}`
  );
});

it("explains pending settings and a failed load without claiming the server is unconfigured", async () => {
  let rejectResponse!: (reason: Error) => void;
  const response = new Promise<Response>((_resolve, reject) => {
    rejectResponse = reject;
  });
  vi.stubGlobal(
    "fetch",
    vi.fn(() => response)
  );
  const container = await mountPushSettings();
  expect(container.textContent).toContain("Loading notification settings");
  expect(container.textContent).not.toContain("not configured on this server");
  expect(
    [...container.querySelectorAll("button")].find((button) => button.textContent === "Register this browser")?.disabled
  ).toBe(true);
  await act(async () => rejectResponse(new Error("Server unavailable")));
  expect(container.textContent).toContain("Could not load notification settings");
  expect(container.textContent).toContain("Server unavailable");
  expect(container.textContent).not.toContain("Loading notification settings");
  expect(container.textContent).not.toContain("not configured on this server");
});
