import { describe, expect, it } from "vitest";

import { clampBlock, firstLines } from "./vocabulary";

describe("firstLines", () => {
  it("keeps the first n non-blank lines and flags that more was dropped", () => {
    expect(firstLines("a\n\nb\nc\nd", 2)).toEqual({ text: "a\nb", truncated: true });
  });

  it("does not flag truncation when every non-blank line fits", () => {
    expect(firstLines("a\n\nb", 3)).toEqual({ text: "a\nb", truncated: false });
  });

  it("clamps a pathologically long single line by character count", () => {
    const long = "x".repeat(5000);
    const { text, truncated } = firstLines(long, 3);
    expect(text.length).toBeLessThan(long.length);
    expect(text.endsWith("…")).toBe(true);
    // A char-clamped-but-single line is not a dropped-lines truncation.
    expect(truncated).toBe(false);
  });
});

describe("clampBlock", () => {
  it("appends an ellipsis line only when the block was truncated", () => {
    expect(clampBlock("a\nb\nc", 2)).toBe("a\nb\n…");
    expect(clampBlock("a\nb", 2)).toBe("a\nb");
  });
});
