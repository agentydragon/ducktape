// @vitest-environment happy-dom
import { MantineProvider } from "@mantine/core";
import { type JSX, act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import { HighlightedText, JsonView } from "./json_view";

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
  await act(async () => root.render(<MantineProvider env="test">{element}</MantineProvider>));
  return container;
}

describe("JsonView", () => {
  it("renders a value in the shared CodeMirror viewer", async () => {
    const container = await render(<JsonView value={{ repository: "test-owner/test-repository", count: 3 }} />);
    expect(container.textContent).toContain('"repository": "test-owner/test-repository"');
    expect(container.querySelector(".cm-editor")).not.toBeNull();
  });

  it("renders untrusted source as inert text", async () => {
    // An untrusted value (agent/tool output) that must never become a live DOM element.
    const container = await render(<JsonView value={{ payload: '<img src=x onerror="alert(1)">' }} />);
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("[onerror]")).toBeNull();
    expect(container.textContent).toContain("<img src=x");
  });
});

describe("HighlightedText", () => {
  it("uses the JSON CodeMirror language for text that looks like JSON", async () => {
    const container = await render(<HighlightedText text={'{"command": "ls"}'} />);
    expect(container.querySelector(".cm-editor")).not.toBeNull();
    expect(container.textContent).toContain('{"command": "ls"}');
  });

  it("renders plain text unchanged rather than tokenizing prose as JSON", async () => {
    const container = await render(<HighlightedText text={"README.md\nsrc\n"} />);
    expect(container.querySelector(".cm-editor")).not.toBeNull();
    expect(container.textContent).toContain("README.md");
  });
});
