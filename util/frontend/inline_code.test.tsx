// @vitest-environment happy-dom
import { MantineProvider } from "@mantine/core";
import { type ReactNode, act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import { InlineCode } from "./code_block";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const mounted: Array<{ root: ReturnType<typeof createRoot>; container: HTMLDivElement }> = [];

async function mount(node: ReactNode): Promise<HTMLDivElement> {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  mounted.push({ root, container });
  await act(async () => root.render(<MantineProvider env="test">{node}</MantineProvider>));
  return container;
}

afterEach(async () => {
  await act(async () => {
    for (const { root } of mounted) root.unmount();
  });
  for (const { container } of mounted) container.remove();
  mounted.length = 0;
});

// The element's own text: the provider that mounts it adds style rules to the container.
const codeText = (container: HTMLElement): string | undefined =>
  container.querySelector("code")?.textContent ?? undefined;

const tokens = (container: HTMLElement, token: string): string[] =>
  [...container.querySelectorAll(`.ui-tok-${token}`)].map((node) => node.textContent ?? "");

describe("InlineCode", () => {
  it("colours shell by the grammar the viewer uses, without changing a character of it", async () => {
    const text = 'if true; then echo "test-output"; fi';
    const container = await mount(<InlineCode text={text} language="bash" />);
    expect(codeText(container)).toBe(text);
    expect(tokens(container, "keyword")).toEqual(expect.arrayContaining(["if", "then", "fi"]));
    expect(tokens(container, "string")).toEqual(['"test-output"']);
  });

  it("marks a hidden character as the viewer does, and does not show it", async () => {
    const container = await mount(<InlineCode text={"ls\u202e-la"} />);
    const marker = container.querySelector(".cm-ui-special-char-bidi");
    expect(marker?.textContent).toBe("⟦RLO⟧");
    expect(marker?.getAttribute("aria-label")).toContain("U+202E");
    expect(codeText(container)).not.toContain("\u202e");
  });

  it("copes with a quote that never closes, as a summary cut short does", async () => {
    const container = await mount(<InlineCode text={'echo "test-cut-off'} language="bash" />);
    expect(codeText(container)).toBe('echo "test-cut-off');
  });

  it("uses the registered language grammar for inline code without an editor", async () => {
    const container = await mount(<InlineCode text={'def greet(): return "hello"'} language="python" />);

    expect(codeText(container)).toBe('def greet(): return "hello"');
    expect(tokens(container, "keyword")).toContain("def");
    expect(container.querySelector(".cm-editor")).toBeNull();
  });

  it("keeps a streaming cursor at the end of the inline code", async () => {
    const container = await mount(<InlineCode text="print(42)" language="python" streamingCursor />);
    const cursor = container.querySelector<HTMLElement>(".ui-streaming-cursor");

    expect(cursor?.getAttribute("aria-label")).toBe("Streaming");
    expect(cursor?.getAttribute("data-character")).toBe("|");
    expect(cursor?.parentElement?.tagName).toBe("CODE");
    expect(codeText(container)).toBe("print(42)");
  });
});
