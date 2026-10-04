// @vitest-environment happy-dom
import { act, type JSX, useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { mount } from "./actions/testing";
import { ClampedBlock, lineCount } from "./clamped_block";

// happy-dom does no layout, so the content's height is whatever a test says it is.
function contentHeight(pixels: number): void {
  vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockReturnValue(pixels);
}

function control(container: HTMLElement, label: string): HTMLElement | undefined {
  return [...container.querySelectorAll<HTMLElement>("button")].find((button) => button.textContent === label);
}

afterEach(() => vi.restoreAllMocks());

describe("ClampedBlock", () => {
  it("leaves content within its cap alone, with nothing to click", async () => {
    contentHeight(40);
    const container = await mount(<ClampedBlock maxHeightRem={10}>test-content</ClampedBlock>);
    expect(container.querySelector('[data-clamped="true"]')).toBeNull();
    expect(container.querySelector("button")).toBeNull();
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

    await act(async () => control(container, "Show less")?.click());
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
    expect(control(container, "Show less")).toBeDefined();
  });
});

describe("lineCount", () => {
  it("counts a final newline as ending a line, not starting one", () => {
    expect([lineCount("a"), lineCount("a\nb"), lineCount("a\nb\n"), lineCount("")]).toEqual([1, 2, 2, 1]);
  });
});
