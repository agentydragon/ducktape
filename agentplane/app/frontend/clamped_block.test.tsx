// @vitest-environment happy-dom
import { screen } from "@testing-library/react";
import { type JSX, useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ClampedBlock, lineCount } from "./clamped_block";
import { renderInMantine } from "./testing_library";

// happy-dom does no layout, so the content's height is whatever a test says it is.
function contentHeight(pixels: number): void {
  vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockReturnValue(pixels);
}

// The clipping box has no role or name of its own; its `data-clamped` is the state's only trace.
function clip(): HTMLElement {
  const box = screen.getByText("test-content").closest<HTMLElement>("[data-clamped]");
  if (!box) throw new Error("missing clipping box");
  return box;
}

afterEach(() => vi.restoreAllMocks());

describe("ClampedBlock", () => {
  it("leaves content within its cap alone, with nothing to click", () => {
    contentHeight(40);
    renderInMantine(<ClampedBlock maxHeightRem={10}>test-content</ClampedBlock>);
    expect(clip()).toHaveAttribute("data-clamped", "false");
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("clips content past its cap, keeping all of it in the document, until the bottom is clicked", async () => {
    contentHeight(1000);
    const { user } = renderInMantine(<ClampedBlock maxHeightRem={10}>test-content</ClampedBlock>);
    expect(clip()).toHaveAttribute("data-clamped", "true");
    // Not `toHaveStyle`: happy-dom's computed style turns `10rem` into `160px`.
    expect(clip().style.maxHeight).toBe("10rem");
    expect(clip()).toHaveTextContent("test-content");

    await user.click(screen.getByRole("button", { name: "Show all" }));
    expect(clip()).toHaveAttribute("data-clamped", "false");
    expect(screen.queryByRole("button", { name: "Show all" })).toBeNull();

    await user.click(screen.getByRole("button", { name: "Show less" }));
    expect(clip()).toHaveAttribute("data-clamped", "true");
  });

  it("says how many lines it hides, when told", () => {
    contentHeight(1000);
    renderInMantine(
      <ClampedBlock maxHeightRem={10} lines={32}>
        test-content
      </ClampedBlock>
    );
    expect(screen.getByRole("button", { name: "Show all 32 lines" })).toBeInTheDocument();
  });

  it("does not count a single line, which can only be one that wraps", () => {
    contentHeight(1000);
    renderInMantine(
      <ClampedBlock maxHeightRem={10} lines={1}>
        test-content
      </ClampedBlock>
    );
    expect(screen.getByRole("button", { name: "Show all" })).toBeInTheDocument();
  });

  it("takes its expansion from the caller when given one", async () => {
    contentHeight(1000);
    const expand = vi.fn();
    const { user } = renderInMantine(
      <ClampedBlock maxHeightRem={10} expansion={[false, expand]}>
        test-content
      </ClampedBlock>
    );
    await user.click(screen.getByRole("button", { name: "Show all" }));
    expect(expand).toHaveBeenCalledWith(true);
    // Still clipped: whether it opens is the caller's to say.
    expect(clip()).toHaveAttribute("data-clamped", "true");
  });

  it("shows retained expansion on mount, as the reader left it", () => {
    contentHeight(1000);
    function Retained(): JSX.Element {
      const expansion = useState(true);
      return (
        <ClampedBlock maxHeightRem={10} expansion={expansion}>
          test-content
        </ClampedBlock>
      );
    }
    renderInMantine(<Retained />);
    expect(clip()).toHaveAttribute("data-clamped", "false");
    expect(screen.getByRole("button", { name: "Show less" })).toBeInTheDocument();
  });
});

describe("lineCount", () => {
  it("counts a final newline as ending a line, not starting one", () => {
    expect([lineCount("a"), lineCount("a\nb"), lineCount("a\nb\n"), lineCount("")]).toEqual([1, 2, 2, 1]);
  });
});
