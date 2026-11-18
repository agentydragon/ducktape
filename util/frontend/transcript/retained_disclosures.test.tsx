// @vitest-environment happy-dom

import { act, type JSX } from "react";
import { afterEach, expect, it } from "vitest";

import { cleanupMounted, mountWithMantine } from "../testing/react";
import { RetainedDisclosure, RetainedDisclosureProvider, useRetainedDisclosure } from "./retained_disclosures";

afterEach(cleanupMounted);

it("restores an open disclosure after its virtualized row remounts", async () => {
  const row = (visible: boolean) => (
    <RetainedDisclosureProvider>
      {visible && (
        <RetainedDisclosure id="item:tool:output" summary="Output">
          <span>selected output</span>
        </RetainedDisclosure>
      )}
    </RetainedDisclosureProvider>
  );
  const { container, rerender } = await mountWithMantine(row(true));
  const render = async (visible: boolean) => rerender(row(visible));

  const control = container.querySelector<HTMLButtonElement>(".ui-disclosure-summary")!;
  await act(async () => control.click());
  expect(container.textContent).toContain("selected output");

  await render(false);
  expect(container.querySelector(".ui-disclosure")).toBeNull();
  await render(true);
  expect(container.querySelector(".ui-disclosure-summary")?.getAttribute("aria-expanded")).toBe("true");
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
  const row = (visible: boolean) => (
    <RetainedDisclosureProvider>{visible && <ButtonDisclosure id="output" defaultOpen />}</RetainedDisclosureProvider>
  );
  const { container, rerender } = await mountWithMantine(row(true));
  const render = async (visible: boolean) => rerender(row(visible));
  const press = async () => act(async () => container.querySelector("button")!.click());

  expect(container.querySelector("button")?.getAttribute("aria-expanded")).toBe("true");
  await press();
  expect(container.querySelector("button")?.getAttribute("aria-expanded")).toBe("false");

  await render(false);
  await render(true);
  expect(container.querySelector("button")?.getAttribute("aria-expanded")).toBe("false");
});

it("restores a button-toggled disclosure after its virtualized row remounts", async () => {
  const row = (visible: boolean) => (
    <RetainedDisclosureProvider>
      {visible && <ButtonDisclosure id="item:message:evidence" />}
    </RetainedDisclosureProvider>
  );
  const { container, rerender } = await mountWithMantine(row(true));
  const render = async (visible: boolean) => rerender(row(visible));
  const press = async () => act(async () => container.querySelector("button")!.click());

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
  const row = (source: string, item: number) => (
    <RetainedDisclosureProvider>
      <RetainedDisclosure id={`${source}:epoch:${item}:evidence`} summary="Evidence">
        <span data-retained-content>{`${source} evidence ${item}`}</span>
      </RetainedDisclosure>
    </RetainedDisclosureProvider>
  );
  const { container, rerender } = await mountWithMantine(row("original", 0));
  const render = async (source: string, item: number) => rerender(row(source, item));

  for (let item = 0; item < 129; item++) {
    await render("original", item);
    const control = container.querySelector<HTMLButtonElement>(".ui-disclosure-summary")!;
    await act(async () => control.click());
    expect(container.querySelector("[data-retained-content]")?.textContent).toBe(`original evidence ${item}`);
  }
  await render("original", 0);
  expect(container.querySelector(".ui-disclosure-summary")?.getAttribute("aria-expanded")).toBe("false");
  expect(container.querySelector("[data-retained-content]")).toBeNull();
  await render("original", 128);
  expect(container.querySelector(".ui-disclosure-summary")?.getAttribute("aria-expanded")).toBe("true");
  await render("replacement", 128);
  expect(container.querySelector(".ui-disclosure-summary")?.getAttribute("aria-expanded")).toBe("false");
  expect(container.querySelector("[data-retained-content]")).toBeNull();
});
