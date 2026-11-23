import { describe, expect, it } from "vitest";

import { clampBlock } from "./vocabulary";

describe("clampBlock", () => {
  it("keeps the first n non-blank lines and appends an ellipsis line only when whole lines were dropped", () => {
    expect(clampBlock("a\n\nb\nc\nd", 2)).toBe("a\nb\n…");
    expect(clampBlock("a\nb", 2)).toBe("a\nb");
    expect(clampBlock("a\n\nb", 3)).toBe("a\nb");
  });

  it("clamps a pathologically long single line by character count", () => {
    const long = "x".repeat(5000);
    const clamped = clampBlock(long, 3);
    expect(clamped.length).toBeLessThan(long.length);
    expect(clamped.endsWith("…")).toBe(true);
    // Clamped by characters, not by dropped lines: no trailing ellipsis line.
    expect(clamped).not.toContain("\n");
  });
});
