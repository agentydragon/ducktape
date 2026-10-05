// @vitest-environment happy-dom

import { screen } from "@testing-library/react";
import type { JSX } from "react";
import { expect, it } from "vitest";

import { renderInMantine } from "../testing_library";
import { RetainedDisclosure, RetainedDisclosureProvider, useRetainedDisclosure } from "./retained_disclosures";

it("restores an open disclosure after its virtualized row remounts", async () => {
  const tree = (visible: boolean) => (
    <RetainedDisclosureProvider>
      {visible && (
        <RetainedDisclosure id="item:tool:output" summary="Output">
          <span>selected output</span>
        </RetainedDisclosure>
      )}
    </RetainedDisclosureProvider>
  );
  const { rerender, user } = renderInMantine(tree(true));

  await user.click(screen.getByText("Output"));
  expect(await screen.findByText("selected output")).toBeInTheDocument();

  rerender(tree(false));
  expect(screen.queryByText("Output")).toBeNull();
  rerender(tree(true));
  expect(screen.getByRole("group")).toHaveAttribute("open");
  expect(screen.getByText("selected output")).toBeInTheDocument();
});

function ButtonDisclosure({ id }: { id: string }): JSX.Element {
  const [open, setOpen] = useRetainedDisclosure(id);
  return (
    <>
      <button aria-expanded={open} onClick={() => setOpen(!open)}>
        Evidence
      </button>
      {open && <span>selected evidence</span>}
    </>
  );
}

it("restores a button-toggled disclosure after its virtualized row remounts", async () => {
  const tree = (visible: boolean) => (
    <RetainedDisclosureProvider>
      {visible && <ButtonDisclosure id="item:message:evidence" />}
    </RetainedDisclosureProvider>
  );
  const { rerender, user } = renderInMantine(tree(true));
  const evidence = () => screen.getByRole("button", { name: "Evidence" });

  await user.click(evidence());
  expect(evidence()).toHaveAttribute("aria-expanded", "true");

  rerender(tree(false));
  expect(screen.queryByRole("button")).toBeNull();
  rerender(tree(true));
  expect(evidence()).toHaveAttribute("aria-expanded", "true");
  expect(screen.getByText("selected evidence")).toBeInTheDocument();

  await user.click(evidence());
  expect(evidence()).toHaveAttribute("aria-expanded", "false");
  expect(screen.queryByText("selected evidence")).toBeNull();
});

it("evicts old disclosure choices and keeps a replacement source closed", async () => {
  const tree = (source: string, item: number) => (
    <RetainedDisclosureProvider>
      <RetainedDisclosure id={`${source}:epoch:${item}:evidence`} summary="Evidence">
        <span>{`${source} evidence ${item}`}</span>
      </RetainedDisclosure>
    </RetainedDisclosureProvider>
  );
  const { rerender, user } = renderInMantine(tree("original", 0));
  const disclosure = () => screen.getByRole("group");

  for (let item = 0; item < 129; item++) {
    rerender(tree("original", item));
    await user.click(screen.getByText("Evidence"));
    expect(await screen.findByText(`original evidence ${item}`)).toBeInTheDocument();
  }
  rerender(tree("original", 0));
  expect(disclosure()).not.toHaveAttribute("open");
  expect(screen.queryByText(/^original evidence/)).toBeNull();
  rerender(tree("original", 128));
  expect(disclosure()).toHaveAttribute("open");
  rerender(tree("replacement", 128));
  expect(disclosure()).not.toHaveAttribute("open");
  expect(screen.queryByText(/evidence 128$/)).toBeNull();
});
