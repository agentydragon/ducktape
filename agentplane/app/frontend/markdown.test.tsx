// @vitest-environment jsdom
// jsdom rather than the usual happy-dom: DOMPurify >= 3.4.8 reads a node's name through the `nodeName`
// getter of `Node.prototype`, which happy-dom answers with "" for every element, so `Markdown`'s
// sanitize drops the outer tag of what it sanitizes (a `<table>` loses its wrapper) -- and every case
// here starts with a different element (h1, pre, ...).
// CLEANUP(added 2026-10-05): Move to happy-dom once its `Node.prototype` `nodeName` getter returns the
//   element's name (`Object.getOwnPropertyDescriptor(Node.prototype, "nodeName").get.call(
//   document.createElement("table"))` is "TABLE"), then drop jsdom from `package.json` and from this
//   package's `vitest_config`.
import { MantineProvider } from "@mantine/core";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import { Markdown, STREAMING_CURSOR } from "./markdown";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
let root: ReturnType<typeof createRoot>;
let container: HTMLDivElement;

async function render(source: string, streaming = false, singleLine = false): Promise<HTMLDivElement> {
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () =>
    root.render(
      <MantineProvider env="test">
        <Markdown source={source} streaming={streaming} singleLine={singleLine} />
      </MantineProvider>
    )
  );
  return container;
}

async function rerender(source: string, streaming = false, singleLine = false): Promise<HTMLDivElement> {
  await act(async () =>
    root.render(
      <MantineProvider env="test">
        <Markdown source={source} streaming={streaming} singleLine={singleLine} />
      </MantineProvider>
    )
  );
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

  it("appends the streaming cursor inline to the final Markdown paragraph", async () => {
    const rendered = await render("The answer is still being written.", true);
    const paragraph = rendered.querySelector(".agentplane-markdown p");
    const cursor = paragraph?.querySelector<HTMLElement>(".agentplane-streaming-cursor");

    expect(cursor?.getAttribute("role")).toBe("img");
    expect(cursor?.getAttribute("aria-label")).toBe("Streaming");
    expect(cursor?.getAttribute("data-character")).toBe(STREAMING_CURSOR);
    expect(paragraph?.lastElementChild).toBe(cursor);
    expect(paragraph?.textContent).toBe("The answer is still being written.");
  });

  // A registered grammar (python), a language with none (elixir) that must not throw, and a bare
  // fence whose angle brackets must stay text.
  it.each([
    ["a registered language", "```python\nimport os\n```", "import os\n"],
    ["an unrecognized language", '```elixir\nIO.puts("hi")\n```', 'IO.puts("hi")\n'],
    ["no language", "```\n<not a real tag>\n```", "<not a real tag>\n"],
  ])("shows a fenced block's text verbatim in the code widget for %s", async (_case, source, text) => {
    const code = (await render(source)).querySelector(".agentplane-code-block");

    expect(code?.querySelector(".cm-editor")).not.toBeNull();
    expect(
      Array.from(code?.querySelectorAll(".cm-line") ?? [])
        .map((line) => line.textContent)
        .join("\n")
    ).toBe(text);
  });

  it("coerces a registered fenced block into one highlighted inline run for a single-line preview", async () => {
    const rendered = await render(
      'Before the example:\n\n```python\ndef greet(name: str) -> str:\n    return "hello"\n```\nAfter the example.',
      false,
      true
    );
    const code = rendered.querySelector<HTMLElement>(".agentplane-markdown--single-line .agentplane-code-inline");

    expect(code).not.toBeNull();
    expect(code?.textContent).toBe('def greet(name: str) -> str: return "hello"');
    expect(code?.querySelector(".agentplane-tok-keyword")?.textContent).toBe("def");
    expect(rendered.querySelector(".agentplane-code-block, .cm-editor")).toBeNull();
  });

  it.each([
    ["an unrecognized language", '```elixir\nIO.puts("hi")\n```', 'IO.puts("hi")'],
    ["no language", "```\n<not a real tag>\n```", "<not a real tag>"],
  ])("shows %s as safe plain inline code", async (_case, source, text) => {
    const rendered = await render(source, false, true);
    const code = rendered.querySelector<HTMLElement>(".agentplane-code-inline");

    expect(code?.textContent).toBe(text);
    expect(code?.querySelector(".agentplane-tok-keyword")).toBeNull();
    expect(rendered.querySelector("script, .agentplane-code-block, .cm-editor")).toBeNull();
  });

  it("keeps the streaming cursor inside an inline fenced-code preview", async () => {
    const rendered = await render("```python\nprint('still streaming')", true, true);
    const code = rendered.querySelector<HTMLElement>(".agentplane-code-inline");
    const cursor = code?.querySelector<HTMLElement>(".agentplane-streaming-cursor");

    expect(cursor?.getAttribute("aria-label")).toBe("Streaming");
    expect(cursor?.getAttribute("data-character")).toBe(STREAMING_CURSOR);
    expect(code?.textContent).toBe("print('still streaming')");
  });

  it("updates a streaming cursor as a fenced code block grows", async () => {
    const initialCode = "a".repeat(34);
    await render(`\`\`\`python\n${initialCode}`, true);

    await rerender(`\`\`\`python\n${initialCode}${"b".repeat(20)}`, true);

    const code = container.querySelector<HTMLElement>(".agentplane-code-block");
    const content = code?.querySelector(".cm-content");
    const cursor = code?.querySelector(".agentplane-streaming-cursor");
    const range = document.createRange();
    if (content && cursor) {
      range.selectNodeContents(content);
      range.setEndBefore(cursor);
    }

    expect(cursor).not.toBeNull();
    expect(range.toString()).toBe(`${initialCode}${"b".repeat(20)}`);
  });

  it("keeps Markdown tables in a horizontally scrollable wrapper", async () => {
    const rendered = await render("| Stage | Value |\n| --- | --- |\n| Parsed | 12.3456 |");
    const table = rendered.querySelector("table");

    expect(table?.parentElement?.className).toBe("agentplane-markdown-table-scroll");
  });

  it("sanitizes hostile HTML and URL schemes while keeping safe Markdown links", async () => {
    const rendered = await render(
      [
        "**Safe emphasis** and [safe link](https://example.test/guide).",
        "",
        "<script>window.__sessionMarkdownFixtureExecuted = true</script>",
        '<img src="x" onerror="window.__sessionMarkdownFixtureExecuted = true">',
        '<a href="javascript:alert(1)" onclick="window.__sessionMarkdownFixtureExecuted = true">unsafe HTML link</a>',
        "[unsafe Markdown link](javascript:alert(1))",
        "[data URL](data:text/html,<script>alert(1)</script>)",
      ].join("\n")
    );

    expect(rendered.querySelector("strong")?.textContent).toBe("Safe emphasis");
    expect(rendered.querySelector('a[href="https://example.test/guide"]')?.textContent).toBe("safe link");
    expect(rendered.querySelector("script, img")).toBeNull();
    expect(rendered.querySelector("[onclick], [onerror]")).toBeNull();
    expect(rendered.querySelector('a[href^="javascript:"], a[href^="data:"]')).toBeNull();
    expect(
      (window as typeof window & { __sessionMarkdownFixtureExecuted?: boolean }).__sessionMarkdownFixtureExecuted
    ).toBeUndefined();
  });
});
