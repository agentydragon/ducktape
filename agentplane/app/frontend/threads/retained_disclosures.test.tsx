// @vitest-environment happy-dom

import { MantineProvider } from "@mantine/core";
import { act, type JSX } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it } from "vitest";

import { RetainedDisclosure, RetainedDisclosureProvider, useRetainedDisclosure } from "./retained_disclosures";

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
        <MantineProvider env="test">
          <RetainedDisclosureProvider>
            {visible && (
              <RetainedDisclosure id="item:tool:output" summary="Output">
                <span>selected output</span>
              </RetainedDisclosure>
            )}
          </RetainedDisclosureProvider>
        </MantineProvider>
      )
    );

  await render(true);
  const control = container.querySelector<HTMLButtonElement>(".agentplane-disclosure-summary")!;
  await act(async () => control.click());
  expect(container.textContent).toContain("selected output");

  await render(false);
  expect(container.querySelector(".agentplane-disclosure")).toBeNull();
  await render(true);
  expect(container.querySelector(".agentplane-disclosure-summary")?.getAttribute("aria-expanded")).toBe("true");
  expect(container.textContent).toContain("selected output");
});

function ButtonDisclosure({ id, defaultOpen = false }: { id: string; defaultOpen?: boolean }): JSX.Element {
  const [open, setOpen] = useRetainedDisclosure(id, defaultOpen);
  return (
    <>
      <button aria-expanded={open} onClick={() => setOpen(!open)}>
        Evidence
      </button>
      {open && <span>selected evidence</span>}
    </>
  );
}

it("uses its default open state until the reader makes a choice", async () => {
  const container = document.createElement("div");
  const root = createRoot(container);
  roots.push(root);
  const render = async (visible: boolean) =>
    act(async () =>
      root.render(
        <MantineProvider env="test">
          <RetainedDisclosureProvider>
            {visible && <ButtonDisclosure id="output" defaultOpen />}
          </RetainedDisclosureProvider>
        </MantineProvider>
      )
    );
  const press = async () => act(async () => container.querySelector("button")!.click());

  await render(true);
  expect(container.querySelector("button")?.getAttribute("aria-expanded")).toBe("true");
  await press();
  expect(container.querySelector("button")?.getAttribute("aria-expanded")).toBe("false");

  await render(false);
  await render(true);
  expect(container.querySelector("button")?.getAttribute("aria-expanded")).toBe("false");
});

it("restores a button-toggled disclosure after its virtualized row remounts", async () => {
  const container = document.createElement("div");
  const root = createRoot(container);
  roots.push(root);
  const render = async (visible: boolean) =>
    act(async () =>
      root.render(
        <MantineProvider env="test">
          <RetainedDisclosureProvider>
            {visible && <ButtonDisclosure id="item:message:evidence" />}
          </RetainedDisclosureProvider>
        </MantineProvider>
      )
    );
  const press = async () => act(async () => container.querySelector("button")!.click());

  await render(true);
  await press();
  expect(container.querySelector("button")?.getAttribute("aria-expanded")).toBe("true");

  await render(false);
  expect(container.querySelector("button")).toBeNull();
  await render(true);
  expect(container.querySelector("button")?.getAttribute("aria-expanded")).toBe("true");
  expect(container.textContent).toContain("selected evidence");

  await press();
  expect(container.querySelector("button")?.getAttribute("aria-expanded")).toBe("false");
  expect(container.querySelector("span")).toBeNull();
});

it("evicts old disclosure choices and keeps a replacement source closed", async () => {
  const container = document.createElement("div");
  const root = createRoot(container);
  roots.push(root);
  const render = async (source: string, item: number) =>
    act(async () =>
      root.render(
        <MantineProvider env="test">
          <RetainedDisclosureProvider>
            <RetainedDisclosure id={`${source}:epoch:${item}:evidence`} summary="Evidence">
              <span data-retained-content>{`${source} evidence ${item}`}</span>
            </RetainedDisclosure>
          </RetainedDisclosureProvider>
        </MantineProvider>
      )
    );

  for (let item = 0; item < 129; item++) {
    await render("original", item);
    const control = container.querySelector<HTMLButtonElement>(".agentplane-disclosure-summary")!;
    await act(async () => control.click());
    expect(container.querySelector("[data-retained-content]")?.textContent).toBe(`original evidence ${item}`);
  }
  await render("original", 0);
  expect(container.querySelector(".agentplane-disclosure-summary")?.getAttribute("aria-expanded")).toBe("false");
  expect(container.querySelector("[data-retained-content]")).toBeNull();
  await render("original", 128);
  expect(container.querySelector(".agentplane-disclosure-summary")?.getAttribute("aria-expanded")).toBe("true");
  await render("replacement", 128);
  expect(container.querySelector(".agentplane-disclosure-summary")?.getAttribute("aria-expanded")).toBe("false");
  expect(container.querySelector("[data-retained-content]")).toBeNull();
});
