// @vitest-environment happy-dom
import { describe, expect, it } from "vitest";

import { isRegisteredLanguage, renderSpecialChar, VISIBLE_SPECIAL_CHARS } from "./code_block";

describe("CodeBlock language support", () => {
  it("recognizes each CodeMirror language grammar used for fenced blocks", () => {
    for (const language of [
      "bash",
      "go",
      "javascript",
      "json",
      "nix",
      "protobuf",
      "python",
      "rust",
      "typescript",
      "yaml",
    ]) {
      expect(isRegisteredLanguage(language)).toBe(true);
    }
    expect(isRegisteredLanguage("unknown")).toBe(false);
  });
});

describe("CodeBlock hidden-character markers", () => {
  const matches = (code: number): boolean =>
    new RegExp(VISIBLE_SPECIAL_CHARS.source, "u").test(String.fromCodePoint(code));

  it("matches bidi controls, zero-width/default-ignorable characters, and C0/C1 controls", () => {
    expect(matches(0x202e)).toBe(true); // RIGHT-TO-LEFT OVERRIDE
    expect(matches(0x200b)).toBe(true); // ZERO WIDTH SPACE
    expect(matches(0x0085)).toBe(true); // NEXT LINE control
    expect(matches(0x001b)).toBe(true); // ESC control
  });

  it("leaves ordinary tabs and line breaks available for layout", () => {
    expect(matches(0x0009)).toBe(false);
    expect(matches(0x000a)).toBe(false);
    expect(matches(0x000d)).toBe(false);
  });

  it("shows a labeled, accessible marker for a bidi override", () => {
    const marker = renderSpecialChar(0x202e, "RIGHT-TO-LEFT OVERRIDE", "");
    expect(marker.textContent).toBe("⟦RLO⟧");
    expect(marker.className).toContain("cm-agentplane-special-char-bidi");
    expect(marker.getAttribute("aria-label")).toContain("U+202E");
    expect(marker.title).toContain("U+202E");
  });

  it("labels zero-width and C0 controls distinctly", () => {
    const zeroWidth = renderSpecialChar(0x200b, null, "");
    const control = renderSpecialChar(0x001b, null, "");
    expect(zeroWidth.textContent).toBe("⟦ZWSP⟧");
    expect(zeroWidth.className).toContain("cm-agentplane-special-char-ignorable");
    expect(control.textContent).toBe("⟦ESC⟧");
    expect(control.className).toContain("cm-agentplane-special-char-control");
  });
});
