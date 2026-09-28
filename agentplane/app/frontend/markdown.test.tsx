// @vitest-environment jsdom
// jsdom rather than this package's usual happy-dom: DOMPurify.sanitize (in `Markdown`) strips the
// tag off the first top-level node of what it sanitizes under happy-dom -- see the note in
// syntax_highlight.test.ts -- and every case here starts with a different element (h1, pre, ...).
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import { Markdown } from "./markdown";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
let root: ReturnType<typeof createRoot>;
let container: HTMLDivElement;

async function render(source: string): Promise<HTMLDivElement> {
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () => root.render(<Markdown source={source} />));
  return container;
}

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
});

describe("Markdown", () => {
  it("leaves plain prose -- headings, lists, links -- unaffected", async () => {
    const rendered = await render("# Title\n\n- one\n- two\n\n[a link](https://example.test/x)");

    expect(rendered.querySelector("h1")?.textContent).toBe("Title");
    expect([...rendered.querySelectorAll("li")].map((li) => li.textContent)).toEqual(["one", "two"]);
    const link = rendered.querySelector("a");
    expect(link?.getAttribute("href")).toBe("https://example.test/x");
    expect(link?.textContent).toBe("a link");
  });

  it("syntax-highlights a fenced code block in a registered language", async () => {
    // "import" is itself the grammar's first highlighted token, so this doesn't depend on any
    // leading plain-text span (see the jsdom-vs-happy-dom note in syntax_highlight.test.ts).
    const rendered = await render("```python\nimport os\n```");

    const code = rendered.querySelector("pre code");
    expect(code?.className).toBe("agentplane-hljs");
    expect(code?.querySelector(".hljs-keyword")?.textContent).toBe("import");
    expect(code?.textContent).toBe("import os\n");
  });

  it("falls back to plain, unhighlighted code for an unrecognized language, without throwing", async () => {
    const rendered = await render('```elixir\nIO.puts("hi")\n```');

    const code = rendered.querySelector("pre code");
    expect(code?.className).toBe("");
    expect(code?.querySelector("span")).toBeNull();
    expect(code?.textContent).toBe('IO.puts("hi")\n');
  });

  it("falls back to plain, unhighlighted code for a fence with no language", async () => {
    const rendered = await render("```\n<not a real tag>\n```");

    const code = rendered.querySelector("pre code");
    expect(code?.className).toBe("");
    expect(code?.querySelector("span")).toBeNull();
    expect(code?.textContent).toBe("<not a real tag>\n");
  });
});
