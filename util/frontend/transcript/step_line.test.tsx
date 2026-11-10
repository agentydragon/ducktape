// @vitest-environment happy-dom

import { MantineProvider } from "@mantine/core";
import { act, type JSX, useState } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it } from "vitest";

import { StepLine } from "./step_line";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const roots: ReturnType<typeof createRoot>[] = [];

afterEach(async () => {
  for (const root of roots.splice(0)) await act(async () => root.unmount());
  document.body.replaceChildren();
});

function InteractiveStep(): JSX.Element {
  const [open, setOpen] = useState(false);
  return (
    <StepLine
      title="Tool"
      preview="inspect workspace"
      summaryAside={<button type="button">Inspect</button>}
      controls={<button type="button">Raw view</button>}
      expandable
      open={open}
      onOpenChange={setOpen}
    >
      <span>tool details</span>
    </StepLine>
  );
}

it("mounts details only when open and keeps the summary action outside the disclosure", async () => {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  roots.push(root);
  await act(async () =>
    root.render(
      <MantineProvider env="test">
        <InteractiveStep />
      </MantineProvider>
    )
  );

  const disclosure = () => container.querySelector<HTMLElement>(".ui-disclosure-summary")!;
  expect(disclosure().getAttribute("aria-expanded")).toBe("false");
  expect(container.textContent).not.toContain("tool details");
  expect(container.textContent).not.toContain("Raw view");

  await act(async () => container.querySelector<HTMLButtonElement>(".transcript-step-inspect button")!.click());
  expect(disclosure().getAttribute("aria-expanded")).toBe("false");
  expect(container.textContent).not.toContain("tool details");

  await act(async () => disclosure().click());
  expect(disclosure().getAttribute("aria-expanded")).toBe("true");
  expect(container.textContent).toContain("tool details");
  expect(container.textContent).toContain("Raw view");
});
