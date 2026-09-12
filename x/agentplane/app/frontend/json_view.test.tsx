// @vitest-environment happy-dom
import { MantineProvider } from "@mantine/core";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import { HighlightedText, JsonView, unwrapMcpContent } from "./json_view";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const mounted: Array<{ root: ReturnType<typeof createRoot>; container: HTMLDivElement }> = [];
afterEach(async () => {
  for (const { root, container } of mounted.splice(0)) {
    await act(async () => root.unmount());
    container.remove();
  }
});

async function render(element: JSX.Element): Promise<HTMLDivElement> {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  mounted.push({ root, container });
  await act(async () => root.render(<MantineProvider>{element}</MantineProvider>));
  return container;
}

describe("JsonView", () => {
  it("renders a value as pretty-printed, syntax-highlighted JSON", async () => {
    const container = await render(<JsonView value={{ repository: "test-owner/test-repository", count: 3 }} />);
    expect(container.textContent).toContain('"repository": "test-owner/test-repository"');
    // highlight.js tokenizes JSON strings/numbers into their own spans; a plain <Code> dump
    // (the pre-existing behavior this replaces) never produced any.
    expect(container.querySelector(".hljs-string")).not.toBeNull();
    expect(container.querySelector(".hljs-number")).not.toBeNull();
  });

  it("sanitizes the highlighted markup: no live tag or attribute, only inert text", async () => {
    // An untrusted value (agent/tool output) that would be a live element if rendered unescaped;
    // highlight.js already HTML-escapes it, and DOMPurify is the belt behind that.
    const container = await render(<JsonView value={{ payload: '<img src=x onerror="alert(1)">' }} />);
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("[onerror]")).toBeNull();
    expect(container.textContent).toContain("<img src=x");
  });
});

describe("HighlightedText", () => {
  it("highlights a string that looks like JSON", async () => {
    const container = await render(<HighlightedText text={'{"command": "ls"}'} />);
    expect(container.querySelector(".hljs-string")).not.toBeNull();
    expect(container.textContent).toContain('{"command": "ls"}');
  });

  it("renders plain, unhighlighted text unchanged rather than tokenizing prose as JSON", async () => {
    const container = await render(<HighlightedText text={"README.md\nsrc\n"} />);
    expect(container.querySelector(".hljs-string")).toBeNull();
    expect(container.textContent).toContain("README.md");
  });
});

describe("unwrapMcpContent", () => {
  it("parses a JSON-encoded string inside an MCP content-block's content array", () => {
    const result = unwrapMcpContent({ content: ['{"login":"test-user","id":714892}'] });
    expect(result).toEqual({ content: [{ login: "test-user", id: 714892 }] });
  });

  it("recurses into a content-block shape nested deeper in the tree", () => {
    const result = unwrapMcpContent({
      outer: { content: ['{"nested":{"content":["{\\"login\\":\\"test-user\\"}"]}}'] },
    });
    expect(result).toEqual({ outer: { content: [{ nested: { content: [{ login: "test-user" }] } }] } });
  });

  it("leaves a value that doesn't match the content-block shape unchanged", () => {
    const plain = { matches: 3, repository: "test-owner/test-repository" };
    expect(unwrapMcpContent(plain)).toEqual(plain);
  });

  it("leaves a content entry that isn't valid JSON as the original string", () => {
    const result = unwrapMcpContent({ content: ["not json"] });
    expect(result).toEqual({ content: ["not json"] });
  });
});
