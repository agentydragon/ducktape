// @vitest-environment happy-dom

import { act, type JSX } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";
import { MantineProvider } from "@mantine/core";
import { QueryClientProvider } from "@tanstack/react-query";

import { EvidencePanel, EvidenceToggle, revealEvidenceOnTap } from "./thread_evidence";
import { createAppQueryClient } from "../query";
import { testItem } from "./testing/entity";
import { ItemKind } from "../../../proto/event_pb";
import { RetainedDisclosureProvider } from "../../../../util/frontend/transcript/retained_disclosures";
import { deferred } from "../../../../util/frontend/testing/deferred";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const mounted: { root: ReturnType<typeof createRoot>; container: HTMLElement }[] = [];
const clients: ReturnType<typeof createAppQueryClient>[] = [];

afterEach(async () => {
  window.getSelection()?.removeAllRanges();
  for (const { root, container } of mounted.splice(0)) {
    await act(async () => root.unmount());
    container.remove();
  }
  for (const client of clients.splice(0)) client.clear();
  vi.unstubAllGlobals();
});

function History(): JSX.Element {
  return (
    <div data-testid="history" onClick={revealEvidenceOnTap}>
      <div className="transcript-inspection-owner" data-testid="first">
        <p>First item</p>
        <button type="button">Control</button>
      </div>
      <div className="transcript-inspection-owner" data-testid="second">
        <p>Second item</p>
      </div>
      <div className="transcript-user-message-row" data-testid="input-row">
        <span data-testid="input-aside">Status</span>
        <div className="transcript-inspection-owner" data-testid="input-bubble">
          Message
        </div>
      </div>
      <div className="transcript-inspection-owner" data-testid="tool-owner">
        <div className="transcript-step-details">
          <button className="ui-disclosure-summary" data-testid="tool-summary">
            Bash
          </button>
        </div>
        <button className="transcript-inspection-toggle">Evidence</button>
      </div>
      <p data-testid="between">Between items</p>
    </div>
  );
}

async function mountHistory(): Promise<(testId: string) => HTMLElement> {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  mounted.push({ root, container });
  await act(async () => root.render(<History />));
  return (testId) => container.querySelector<HTMLElement>(`[data-testid="${testId}"]`)!;
}

const revealed = (find: (testId: string) => HTMLElement): string[] =>
  ["first", "second"].filter((testId) => find(testId).hasAttribute("data-inspection-revealed"));

const tap = (element: HTMLElement): Promise<void> => act(async () => element.click());

it("marks the tapped item, moves the mark to the next one tapped, and clears it on a tap on nothing", async () => {
  const find = await mountHistory();

  await tap(find("first"));
  expect(revealed(find)).toEqual(["first"]);
  await tap(find("first"));
  expect(revealed(find)).toEqual(["first"]);
  await tap(find("second"));
  expect(revealed(find)).toEqual(["second"]);
  await tap(find("between"));
  expect(revealed(find)).toEqual([]);
});

it("leaves the mark alone for a tap on a control, or one that ends a text selection", async () => {
  const find = await mountHistory();
  await tap(find("first"));

  await tap(find("first").querySelector("button")!);
  expect(revealed(find)).toEqual(["first"]);

  window.getSelection()?.selectAllChildren(find("second"));
  await tap(find("second"));
  expect(revealed(find)).toEqual(["first"]);
});

it("reveals the tool inspector when a touch tap opens its disclosure heading", async () => {
  const find = await mountHistory();

  await tap(find("tool-summary"));

  expect(find("tool-owner").hasAttribute("data-inspection-revealed")).toBe(true);
});

it("selects a message bubble when the adjacent command gutter is tapped", async () => {
  const find = await mountHistory();
  await tap(find("input-aside"));
  expect(find("input-bubble").hasAttribute("data-inspection-revealed")).toBe(true);
  await tap(find("second"));
  expect(find("input-bubble").hasAttribute("data-inspection-revealed")).toBe(false);
});

async function mountEvidence(threadId: string) {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  mounted.push({ root, container });
  const client = createAppQueryClient();
  clients.push(client);
  const entity = testItem(1, ItemKind.ASSISTANT_TEXT);
  async function render(id: string): Promise<void> {
    await act(async () =>
      root.render(
        <QueryClientProvider client={client}>
          <MantineProvider env="test">
            <RetainedDisclosureProvider>
              <EvidenceToggle entity={entity} />
              <EvidencePanel threadId={id} entity={entity} />
            </RetainedDisclosureProvider>
          </MantineProvider>
        </QueryClientProvider>
      )
    );
  }
  await render(threadId);
  return { container, render };
}

function control(container: HTMLElement, text: string): HTMLButtonElement {
  const button = [...container.querySelectorAll("button")].find((node) => node.textContent?.includes(text));
  if (button === undefined) throw new Error(`missing ${text} control`);
  return button;
}

it("loads evidence and raw frames lazily, retains failed pages, and preserves uint64 cursors", async () => {
  const requests: Request[] = [];
  const laterPage = deferred<Response>();
  const observation = "9007199254740993";
  let rawAttempts = 0;
  vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
    const request = new Request(input, init);
    requests.push(request.clone());
    const body = (await request.json()) as { afterCursor?: string };
    if (request.url.endsWith("/native-frames")) {
      rawAttempts++;
      return rawAttempts === 1
        ? Response.json({ detail: "raw unavailable" }, { status: 503 })
        : Response.json({ frames: [{ sourceSequence: observation }] });
    }
    if (body.afterCursor === observation) return laterPage.promise;
    return Response.json({
      observations: [{ observationCursor: observation, hasNative: true }],
      nextAfterCursor: observation,
    });
  });
  const { container } = await mountEvidence("test-thread");
  expect(requests).toHaveLength(0);
  await tap(container.querySelector<HTMLButtonElement>('button[aria-label="Evidence"]')!);
  await vi.waitFor(() =>
    expect(container.querySelector(`[data-evidence-observation="${observation}"]`)).not.toBeNull()
  );
  expect(requests).toHaveLength(1);
  await tap(control(container, "Load more evidence"));
  expect(container.querySelector(`[data-evidence-observation="${observation}"]`)).not.toBeNull();
  await act(async () => laterPage.resolve(Response.json({ detail: "later page unavailable" }, { status: 503 })));
  await vi.waitFor(() => expect(container.textContent).toContain("later page unavailable"));
  expect(container.querySelector(`[data-evidence-observation="${observation}"]`)).not.toBeNull();
  expect(await requests[1]!.json()).toMatchObject({ afterCursor: observation, projectionEpoch: "test-epoch" });

  await tap(control(container, `Observation ${observation} raw frames`));
  await vi.waitFor(() => expect(container.textContent).toContain("raw unavailable"));
  expect(rawAttempts).toBe(1);
  await tap(control(container, "Retry raw frames"));
  await vi.waitFor(() => expect(container.textContent).toContain(`Raw frame ${observation} unavailable`));
  expect(container.textContent).not.toContain("raw unavailable");
  expect(await requests.at(-1)!.json()).toMatchObject({ observationCursor: observation });
});

it("aborts an old session's read and ignores its late response after switching sessions", async () => {
  const oldResponse = deferred<Response>();
  const requests: Request[] = [];
  vi.stubGlobal("fetch", (input: RequestInfo | URL, init?: RequestInit) => {
    const request = new Request(input, init);
    requests.push(request);
    return request.url.includes("/old-thread/")
      ? oldResponse.promise
      : Promise.resolve(Response.json({ observations: [{ observationCursor: "22", hasNative: false }] }));
  });
  const { container, render } = await mountEvidence("old-thread");
  await tap(container.querySelector<HTMLButtonElement>('button[aria-label="Evidence"]')!);
  expect(requests).toHaveLength(1);
  await render("new-thread");
  await vi.waitFor(() => expect(container.querySelector('[data-evidence-observation="22"]')).not.toBeNull());
  expect(requests[0]!.signal.aborted).toBe(true);
  await act(async () =>
    oldResponse.resolve(Response.json({ observations: [{ observationCursor: "11", hasNative: false }] }))
  );
  expect(container.querySelector('[data-evidence-observation="11"]')).toBeNull();
  expect(container.querySelector('[data-evidence-observation="22"]')).not.toBeNull();
});
