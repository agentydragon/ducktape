// @vitest-environment happy-dom

import { act, type JSX, useState } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it } from "vitest";

import { ResizeHandle, usePaneSize } from "./resizable_pane";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const STORAGE_KEY = "test-resizable-pane-size";
let root: ReturnType<typeof createRoot> | null = null;
let container: HTMLDivElement | null = null;

function Pane({ effectiveMax }: { effectiveMax: number }): JSX.Element {
  const pane = usePaneSize(STORAGE_KEY, 100, 80, 320, effectiveMax);
  const [resizing, setResizing] = useState(false);
  return (
    <>
      <output data-pane-size={pane.size} data-resizing={resizing} />
      <ResizeHandle
        pane={pane}
        axis="y"
        aria-label="Resize test pane"
        onResizeStart={() => setResizing(true)}
        onResizeEnd={() => setResizing(false)}
      />
    </>
  );
}

async function render(effectiveMax: number): Promise<HTMLDivElement> {
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () => root?.render(<Pane effectiveMax={effectiveMax} />));
  return container;
}

function pointerEvent(type: string, clientY: number): MouseEvent {
  const event = new MouseEvent(type, { bubbles: true, button: 0, clientY });
  Object.defineProperty(event, "pointerId", { value: 7 });
  Object.defineProperty(event, "isPrimary", { value: true });
  return event;
}

function allowPointerCapture(element: HTMLElement): void {
  Object.defineProperty(element, "setPointerCapture", { configurable: true, value: () => {} });
}

afterEach(async () => {
  if (root) await act(async () => root?.unmount());
  container?.remove();
  root = null;
  container = null;
  window.localStorage.removeItem(STORAGE_KEY);
});

it("clamps the displayed pane to the layout and restores its preferred size when space returns", async () => {
  window.localStorage.setItem(STORAGE_KEY, "240");
  let mounted = await render(160);
  let separator = mounted.querySelector<HTMLElement>('[role="separator"]')!;
  expect(separator.getAttribute("aria-valuenow")).toBe("160");
  expect(separator.getAttribute("aria-valuemax")).toBe("160");
  expect(window.localStorage.getItem(STORAGE_KEY)).toBe("240");

  await act(async () => root?.render(<Pane effectiveMax={320} />));
  mounted = container!;
  separator = mounted.querySelector<HTMLElement>('[role="separator"]')!;
  expect(separator.getAttribute("aria-valuenow")).toBe("240");
  expect(window.localStorage.getItem(STORAGE_KEY)).toBe("240");

  await act(async () => {
    separator.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowUp", bubbles: true }));
    separator.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowUp", bubbles: true }));
  });
  expect(separator.getAttribute("aria-valuenow")).toBe("272");
  expect(window.localStorage.getItem(STORAGE_KEY)).toBe("272");

  await act(async () => separator.dispatchEvent(new KeyboardEvent("keydown", { key: "Home", bubbles: true })));
  expect(separator.getAttribute("aria-valuenow")).toBe("80");
  await act(async () => separator.dispatchEvent(new KeyboardEvent("keydown", { key: "End", bubbles: true })));
  expect(separator.getAttribute("aria-valuenow")).toBe("320");
});

it("uses axis direction for dragging and clears pointer state on cancel", async () => {
  const mounted = await render(320);
  const separator = mounted.querySelector<HTMLElement>('[role="separator"]')!;
  allowPointerCapture(separator);

  await act(async () => separator.dispatchEvent(pointerEvent("pointerdown", 100)));
  expect(mounted.querySelector("output")?.getAttribute("data-resizing")).toBe("true");
  await act(async () => separator.dispatchEvent(pointerEvent("pointermove", 80)));
  expect(separator.getAttribute("aria-valuenow")).toBe("120");
  await act(async () => separator.dispatchEvent(pointerEvent("pointercancel", 80)));
  expect(mounted.querySelector("output")?.getAttribute("data-resizing")).toBe("false");

  await act(async () => separator.dispatchEvent(pointerEvent("pointermove", 20)));
  expect(separator.getAttribute("aria-valuenow")).toBe("120");
});
