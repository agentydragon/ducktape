// @vitest-environment happy-dom
import { MantineProvider } from "@mantine/core";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import { CallToolResultView, parseCallToolResult } from "./call_tool_result";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const mounted: Array<{ root: ReturnType<typeof createRoot>; container: HTMLDivElement }> = [];
afterEach(async () => {
  for (const { root, container } of mounted.splice(0)) {
    await act(async () => root.unmount());
    container.remove();
  }
});

/** `stored`, a result as the Action Service keeps it, decoded and drawn. */
async function render(stored: unknown): Promise<HTMLDivElement> {
  const result = parseCallToolResult(stored);
  if (result === null) throw new Error("the fixture is not a CallToolResult");
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  mounted.push({ root, container });
  await act(async () =>
    root.render(
      <MantineProvider env="test">
        <CallToolResultView result={result} />
      </MantineProvider>
    )
  );
  return container;
}

// base64 of "test-image-bytes": nothing here decodes it.
const IMAGE_DATA = "dGVzdC1pbWFnZS1ieXRlcw==";

describe("CallToolResultView", () => {
  it("renders an image block inline from an image/* data URI", async () => {
    const container = await render({
      content: [{ type: "image", data: IMAGE_DATA, mimeType: "image/png" }],
      isError: false,
    });
    expect(container.querySelector("img")?.getAttribute("src")).toBe(`data:image/png;base64,${IMAGE_DATA}`);
  });

  it("gives an <img> no data URI of another type, showing that block as its JSON", async () => {
    const container = await render({
      content: [{ type: "image", data: IMAGE_DATA, mimeType: "text/html" }],
      isError: false,
    });
    expect(container.querySelector("img")).toBeNull();
    expect(container.textContent).toContain('"mimeType": "text/html"');
  });

  it("renders a text block that parses as JSON as structure", async () => {
    const container = await render({
      content: [{ type: "text", text: '{"total_count":1,"items":[{"path":"test/path.py"}]}' }],
      isError: false,
    });
    // Re-serialized, so pretty-printed: the one-line string as stored is not what shows.
    expect(container.textContent).toContain('"total_count": 1');
    expect(container.querySelector(".hljs-attr")).not.toBeNull();
  });

  it("renders a text block that is not JSON as its text, never as markup", async () => {
    const text = 'Echo: <b>test</b> <img src=x onerror="alert(1)">';
    const container = await render({ content: [{ type: "text", text }], isError: false });
    expect(container.textContent).toContain(text);
    expect(container.querySelector("b, img")).toBeNull();
    expect(container.querySelector(".hljs-string")).toBeNull();
  });

  it("flags a result the tool marked as an error", async () => {
    const failed = await render({ content: [{ type: "text", text: "test tool failure" }], isError: true });
    expect(failed.textContent).toContain("Tool error");
    const answered = await render({ content: [{ type: "text", text: "test tool answer" }], isError: false });
    expect(answered.textContent).not.toContain("Tool error");
  });

  it("renders structured content as JSON after the content blocks", async () => {
    const container = await render({
      content: [{ type: "text", text: "test caption" }],
      structuredContent: { test_width: 32 },
      isError: false,
    });
    expect(container.textContent).toMatch(/test caption.*Structured content.*"test_width": 32/s);
  });

  it("links a resource link only when it is a web URL", async () => {
    const container = await render({
      content: [
        { type: "resource_link", uri: "https://test-docs.example/page", name: "test-page" },
        { type: "resource_link", uri: "javascript:alert(1)", name: "test-script" },
      ],
      isError: false,
    });
    const links = [...container.querySelectorAll("a")];
    expect(links.map((link) => link.getAttribute("href"))).toEqual(["https://test-docs.example/page"]);
    expect(links[0].getAttribute("rel")).toBe("noreferrer");
    expect(container.textContent).toContain("javascript:alert(1)");
  });

  it("shows an embedded text resource's text under its URI, and any block it does not draw as JSON", async () => {
    const container = await render({
      content: [
        { type: "resource", resource: { uri: "test://resource/text", mimeType: "text/plain", text: "test file text" } },
        { type: "resource", resource: { uri: "test://resource/blob", blob: IMAGE_DATA } },
        { type: "test_future_kind", detail: "test-detail" },
      ],
      isError: false,
    });
    expect(container.textContent).toMatch(/test:\/\/resource\/text.*test file text/s);
    expect(container.textContent).toContain(`"blob": "${IMAGE_DATA}"`);
    expect(container.textContent).toContain('"type": "test_future_kind"');
  });
});

describe("parseCallToolResult", () => {
  it("is null for a value that is not a CallToolResult", () => {
    for (const value of [{ test: true }, { content: "test" }, { content: [], isError: "test" }, [], "test", null]) {
      expect(parseCallToolResult(value)).toBeNull();
    }
  });
});
