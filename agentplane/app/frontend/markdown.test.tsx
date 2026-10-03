// @vitest-environment jsdom
// jsdom rather than this package's usual happy-dom: DOMPurify.sanitize (in `Markdown`) strips the
// tag off the first top-level node of what it sanitizes under happy-dom -- see the note in
// code_block.test.ts -- and every case here starts with a different element (h1, pre, ...).
import { MantineProvider } from "@mantine/core";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import { Markdown, STREAMING_CURSOR } from "./markdown";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
let root: ReturnType<typeof createRoot>;
let container: HTMLDivElement;

async function render(source: string, streaming = false): Promise<HTMLDivElement> {
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () =>
    root.render(
      <MantineProvider env="test">
        <Markdown source={source} streaming={streaming} />
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

  it("uses the CodeMirror language extension for a registered fenced block", async () => {
    const rendered = await render("```python\nimport os\n```");

    const code = rendered.querySelector(".agentplane-code-block");
    expect(code).not.toBeNull();
    expect(code?.querySelector(".cm-editor")).not.toBeNull();
    expect(
      Array.from(code?.querySelectorAll(".cm-line") ?? [])
        .map((line) => line.textContent)
        .join("\n")
    ).toBe("import os\n");
  });

  it("falls back to plain, unhighlighted code for an unrecognized language, without throwing", async () => {
    const rendered = await render('```elixir\nIO.puts("hi")\n```');

    const code = rendered.querySelector(".agentplane-code-block");
    expect(code?.querySelector(".cm-editor")).not.toBeNull();
    expect(
      Array.from(code?.querySelectorAll(".cm-line") ?? [])
        .map((line) => line.textContent)
        .join("\n")
    ).toBe('IO.puts("hi")\n');
  });

  it("falls back to plain, unhighlighted code for a fence with no language", async () => {
    const rendered = await render("```\n<not a real tag>\n```");

    const code = rendered.querySelector(".agentplane-code-block");
    expect(code?.querySelector(".cm-editor")).not.toBeNull();
    expect(
      Array.from(code?.querySelectorAll(".cm-line") ?? [])
        .map((line) => line.textContent)
        .join("\n")
    ).toBe("<not a real tag>\n");
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
