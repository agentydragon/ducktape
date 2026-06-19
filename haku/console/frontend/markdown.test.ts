import { describe, expect, test } from "vitest";

import { renderMarkdown } from "./markdown.ts";

describe("renderMarkdown", () => {
  test("renders bold, inline code, and links", () => {
    const html = renderMarkdown("**why** it matters with `code` and a [link](https://example.com/x).");
    expect(html).toContain("<strong>why</strong>");
    expect(html).toContain("<code>code</code>");
    expect(html).toContain('href="https://example.com/x"');
  });

  test("sanitizes embedded HTML (no script execution surface)", () => {
    expect(renderMarkdown("hi <script>alert(1)</script>")).not.toContain("<script>");
  });
});
