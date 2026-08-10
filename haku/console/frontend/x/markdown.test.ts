import { describe, expect, it } from "vitest";

import { sanitizedMarkdown } from "./markdown";

describe("sanitizedMarkdown", () => {
  it("renders common Markdown", () => {
    const html = sanitizedMarkdown("**Important**\n\n- first\n- second\n\n`code`");

    expect(html).toContain("<strong>Important</strong>");
    expect(html).toContain("<li>first</li>");
    expect(html).toContain("<code>code</code>");
  });

  it("allows links while removing active and presentational raw HTML", () => {
    const html = sanitizedMarkdown(
      '[documentation](https://example.com "Docs")<img src="https://tracker.example/pixel" onerror="alert(1)"><span style="position:fixed">safe</span><script>alert(2)</script>'
    );

    expect(html).toContain('<a href="https://example.com" title="Docs">documentation</a>');
    expect(html).not.toContain("<img");
    expect(html).not.toContain("<span");
    expect(html).not.toContain("style=");
    expect(html).not.toContain("onerror");
    expect(html).not.toContain("<script");
    expect(html).toContain("safe");
  });
});
