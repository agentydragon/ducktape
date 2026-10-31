// @vitest-environment happy-dom
import { MantineProvider } from "@mantine/core";
import { act, type JSX, useState } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ClampedBlock, lineCount } from "./clamped_block";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const roots: ReturnType<typeof createRoot>[] = [];

async function mount(node: JSX.Element): Promise<HTMLDivElement> {
  const container = document.createElement("div");
  const root = createRoot(container);
  roots.push(root);
  await act(async () => root.render(<MantineProvider env="test">{node}</MantineProvider>));
  return container;
}

// happy-dom does no layout, so the content's height is whatever a test says it is.
function contentHeight(pixels: number): void {
  vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockReturnValue(pixels);
}

function control(container: HTMLElement, label: string): HTMLElement | undefined {
  return [...container.querySelectorAll<HTMLElement>("button")].find((button) => button.textContent === label);
}

afterEach(async () => {
  for (const root of roots.splice(0)) await act(async () => root.unmount());
  vi.restoreAllMocks();
});

describe("ClampedBlock", () => {
  it("tracks content size through expansion and collapse", async () => {
    contentHeight(1000);
    const observe = vi.spyOn(ResizeObserver.prototype, "observe");
    const container = await mount(<ClampedBlock maxHeightRem={10}>test-content</ClampedBlock>);
    for (const label of ["Show all", "Expanded content", "Show all"]) {
      await act(async () => control(container, label)?.click());
      const content = container.querySelector("[data-clamped] > div");
      expect(content?.isConnected).toBe(true);
      expect(observe.mock.calls.some(([element]) => element === content)).toBe(true);
    }
  });

  it("leaves content within its cap alone, with nothing to click", async () => {
    contentHeight(40);
    const container = await mount(<ClampedBlock maxHeightRem={10}>test-content</ClampedBlock>);
    expect(container.querySelector('[data-clamped="true"]')).toBeNull();
    expect(control(container, "Show all")).toBeUndefined();
    expect(container.querySelector('.agentplane-clamped-block[data-expanded="true"]')).toBeNull();
  });

  it("keeps expanded content collapsible when its height shrinks", async () => {
    let height = 1000;
    vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockImplementation(() => height);
    const container = await mount(<ClampedBlock maxHeightRem={10}>test-content</ClampedBlock>);
    height = 40;
    await act(async () => control(container, "Show all")?.click());
    expect(control(container, "Expanded content")).toBeDefined();
    await act(async () => control(container, "Expanded content")?.click());
    expect(container.querySelector('.agentplane-clamped-block[data-expanded="true"]')).toBeNull();
  });

  it("clips content past its cap, keeping all of it in the document, until the bottom is clicked", async () => {
    contentHeight(1000);
    const container = await mount(<ClampedBlock maxHeightRem={10}>test-content</ClampedBlock>);
    const clip = container.querySelector<HTMLElement>('[data-clamped="true"]');
    expect(clip?.style.maxHeight).toBe("10rem");
    expect(clip?.textContent).toContain("test-content");

    await act(async () => control(container, "Show all")?.click());
    expect(container.querySelector('[data-clamped="true"]')).toBeNull();
    expect(control(container, "Show all")).toBeUndefined();

    expect(control(container, "Expanded content")?.getAttribute("aria-expanded")).toBe("true");
    expect(container.querySelector(".agentplane-clamped-disclosure")?.textContent).toContain("test-content");
    await act(async () => control(container, "Expanded content")?.click());
    expect(container.querySelector('[data-clamped="true"]')).not.toBeNull();
  });

  it("keeps an existing heading in normal flow until it becomes a sticky collapse row", async () => {
    contentHeight(1000);
    const container = await mount(
      <ClampedBlock maxHeightRem={10} label="Output" header={<span className="agentplane-output-label">Output</span>}>
        test-content
      </ClampedBlock>
    );
    expect(container.querySelector(".agentplane-output-label")?.textContent).toBe("Output");
    expect(container.querySelector(".agentplane-clamped-disclosure")).not.toBeNull();
    expect(container.querySelector(".agentplane-clamped-disclosure .agentplane-output-label")?.textContent).toBe(
      "Output"
    );
    await act(async () => control(container, "Show all")?.click());
    expect(container.querySelector(".agentplane-clamped-disclosure .agentplane-output-label")?.textContent).toBe(
      "Output"
    );
    const heading = container.querySelector(".agentplane-clamped-disclosure .agentplane-disclosure-heading");
    expect(heading?.querySelectorAll("button")).toHaveLength(1);
    expect(control(container, "Output")?.getAttribute("aria-expanded")).toBe("true");
    await act(async () => control(container, "Output")?.click());
    expect(container.querySelector('[data-clamped="true"]')).not.toBeNull();
  });

  it("says how many lines it hides, when told", async () => {
    contentHeight(1000);
    const container = await mount(
      <ClampedBlock maxHeightRem={10} lines={32}>
        test-content
      </ClampedBlock>
    );
    expect(control(container, "Show all 32 lines")).toBeDefined();
  });

  it("does not count a single line, which can only be one that wraps", async () => {
    contentHeight(1000);
    const container = await mount(
      <ClampedBlock maxHeightRem={10} lines={1}>
        test-content
      </ClampedBlock>
    );
    expect(control(container, "Show all")).toBeDefined();
  });

  it("uses a semantic expansion label when the clipped content is not a counted text payload", async () => {
    contentHeight(1000);
    const container = await mount(
      <ClampedBlock maxHeightRem={10} label="Payload" expandLabel="Show more">
        test-content
      </ClampedBlock>
    );
    expect(control(container, "Show more")).toBeDefined();
  });

  it("returns nested output to its clipped preview with Show less", async () => {
    contentHeight(1000);
    const container = await mount(
      <ClampedBlock maxHeightRem={10} stickyCollapse={false} label="Output">
        test-output
      </ClampedBlock>
    );
    await act(async () => control(container, "Show all")!.click());
    expect(container.querySelector('[data-clamped="true"]')).toBeNull();
    await act(async () => control(container, "Show less")!.click());
    expect(container.querySelector('[data-clamped="true"]')).not.toBeNull();
    expect(control(container, "Show all")).toBeDefined();
  });

  it("takes its expansion from the caller when given one", async () => {
    contentHeight(1000);
    const expand = vi.fn();
    const container = await mount(
      <ClampedBlock maxHeightRem={10} expansion={[false, expand]}>
        test-content
      </ClampedBlock>
    );
    await act(async () => control(container, "Show all")?.click());
    expect(expand).toHaveBeenCalledWith(true);
    // Still clipped: whether it opens is the caller's to say.
    expect(container.querySelector('[data-clamped="true"]')).not.toBeNull();
  });

  it("shows retained expansion on mount, as the reader left it", async () => {
    contentHeight(1000);
    function Retained(): JSX.Element {
      const expansion = useState(true);
      return (
        <ClampedBlock maxHeightRem={10} expansion={expansion}>
          test-content
        </ClampedBlock>
      );
    }
    const container = await mount(<Retained />);
    expect(container.querySelector('[data-clamped="true"]')).toBeNull();
    expect(control(container, "Expanded content")).toBeDefined();
  });
});

describe("lineCount", () => {
  it("counts a final newline as ending a line, not starting one", () => {
    expect([lineCount("a"), lineCount("a\nb"), lineCount("a\nb\n"), lineCount("")]).toEqual([1, 2, 2, 1]);
  });
});
