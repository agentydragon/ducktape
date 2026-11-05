// @vitest-environment happy-dom
import { afterEach, describe, expect, it } from "vitest";

import { HighlightedText, JsonView } from "./json_view";
import { cleanupMounted, renderWithMantine } from "./testing/react";

afterEach(cleanupMounted);

describe("JsonView", () => {
  it("renders untrusted source as inert text", async () => {
    // An untrusted value (agent/tool output) that must never become a live DOM element.
    const container = await renderWithMantine(<JsonView value={{ payload: '<img src=x onerror="alert(1)">' }} />);
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("[onerror]")).toBeNull();
    expect(container.textContent).toContain("<img src=x");
  });

  it("keeps JSON framed by default and forwards a plain surface for highlighted text", async () => {
    const container = await renderWithMantine(
      <div>
        <JsonView value={{ status: "ok" }} />
        <HighlightedText text='{"status":"ok"}' surface="plain" />
      </div>
    );
    const surfaces = [...container.querySelectorAll(".ui-code-surface-framed, .ui-code-surface-plain")];
    expect(surfaces).toHaveLength(2);
    expect(surfaces[0]?.classList.contains("ui-code-surface-framed")).toBe(true);
    expect(surfaces[1]?.classList.contains("ui-code-surface-plain")).toBe(true);
  });
});
