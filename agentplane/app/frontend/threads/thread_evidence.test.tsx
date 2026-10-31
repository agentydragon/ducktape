// @vitest-environment happy-dom

import { act, type JSX } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it } from "vitest";

import { revealEvidenceOnTap } from "./thread_evidence";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const mounted: { root: ReturnType<typeof createRoot>; container: HTMLElement }[] = [];

afterEach(async () => {
  window.getSelection()?.removeAllRanges();
  for (const { root, container } of mounted.splice(0)) {
    await act(async () => root.unmount());
    container.remove();
  }
});

function History(): JSX.Element {
  return (
    <div data-testid="history" onClick={revealEvidenceOnTap}>
      <div className="agentplane-evidence-owner" data-testid="first">
        <p>First item</p>
        <button type="button">Control</button>
      </div>
      <div className="agentplane-evidence-owner" data-testid="second">
        <p>Second item</p>
      </div>
      <div className="transcript-user-message-row" data-testid="input-row">
        <span data-testid="input-aside">Status</span>
        <div className="agentplane-evidence-owner" data-testid="input-bubble">
          Message
        </div>
      </div>
      <div className="agentplane-evidence-owner" data-testid="tool-owner">
        <div className="agentplane-step-details">
          <button className="ui-disclosure-summary" data-testid="tool-summary">
            Bash
          </button>
        </div>
        <button className="agentplane-evidence-toggle">Evidence</button>
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
  ["first", "second"].filter((testId) => find(testId).hasAttribute("data-evidence-revealed"));

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

  expect(find("tool-owner").hasAttribute("data-evidence-revealed")).toBe(true);
});

it("selects a message bubble when the adjacent command gutter is tapped", async () => {
  const find = await mountHistory();
  await tap(find("input-aside"));
  expect(find("input-bubble").hasAttribute("data-evidence-revealed")).toBe(true);
  await tap(find("second"));
  expect(find("input-bubble").hasAttribute("data-evidence-revealed")).toBe(false);
});
