// @vitest-environment happy-dom

import { act, type JSX, useState } from "react";
import { afterEach, expect, it } from "vitest";

import { cleanupMounted, renderWithMantine } from "../testing/react";
import { StepLine } from "./step_line";

afterEach(cleanupMounted);

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
  const container = await renderWithMantine(<InteractiveStep />);

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
