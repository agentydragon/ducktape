// @vitest-environment happy-dom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it } from "vitest";

import { RetainedDisclosure, RetainedDisclosureProvider } from "./retained_disclosures";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const roots: ReturnType<typeof createRoot>[] = [];

afterEach(async () => {
  for (const root of roots.splice(0)) await act(async () => root.unmount());
});

it("restores an open disclosure after its virtualized row remounts", async () => {
  const container = document.createElement("div");
  const root = createRoot(container);
  roots.push(root);
  const render = async (visible: boolean) =>
    act(async () =>
      root.render(
        <RetainedDisclosureProvider>
          {visible && (
            <RetainedDisclosure id="item:tool:output" summary="Output">
              <span>selected output</span>
            </RetainedDisclosure>
          )}
        </RetainedDisclosureProvider>
      )
    );

  await render(true);
  const details = container.querySelector("details")!;
  await act(async () => {
    details.open = true;
    details.dispatchEvent(new Event("toggle"));
  });
  expect(container.textContent).toContain("selected output");

  await render(false);
  expect(container.querySelector("details")).toBeNull();
  await render(true);
  expect(container.querySelector("details")?.open).toBe(true);
  expect(container.textContent).toContain("selected output");
});
