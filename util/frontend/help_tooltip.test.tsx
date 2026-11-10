// @vitest-environment happy-dom
import { act } from "react";
import { afterEach, describe, expect, it } from "vitest";

import { HelpTooltip } from "./help_tooltip";
import { cleanupMounted, renderWithMantine } from "./testing/react";

afterEach(cleanupMounted);

async function mount(): Promise<HTMLDivElement> {
  return renderWithMantine(<HelpTooltip topic="Spend policy" description={"First line.\nSecond line."} />);
}

describe("HelpTooltip", () => {
  it("opens for keyboard focus and closes with Escape", async () => {
    const container = await mount();
    const button = container.querySelector<HTMLButtonElement>("button");

    expect(button?.getAttribute("aria-label")).toBe("About Spend policy");
    expect(button?.getAttribute("title")).toBeNull();
    await act(async () => button?.focus());

    const tooltip = document.querySelector<HTMLElement>('[role="tooltip"]');
    expect(tooltip?.textContent).toBe("First line.\nSecond line.");
    expect(button?.getAttribute("aria-describedby")).toBe(tooltip?.id);

    await act(async () => document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
    expect(document.querySelector('[role="tooltip"]')).toBeNull();
  });

  it("opens on hover and closes when the pointer leaves", async () => {
    const container = await mount();
    const button = container.querySelector<HTMLButtonElement>("button");

    await act(async () =>
      button?.dispatchEvent(new PointerEvent("pointerover", { bubbles: true, pointerType: "mouse" }))
    );
    expect(document.querySelector('[role="tooltip"]')?.textContent).toBe("First line.\nSecond line.");

    await act(async () =>
      button?.dispatchEvent(new PointerEvent("pointerout", { bubbles: true, pointerType: "mouse" }))
    );
    expect(document.querySelector('[role="tooltip"]')).toBeNull();
  });

  it("pins open on tap and dismisses on an outside pointer", async () => {
    const container = await mount();
    const button = container.querySelector<HTMLButtonElement>("button");

    await act(async () => {
      button?.click();
    });
    expect(document.querySelector('[role="tooltip"]')).not.toBeNull();

    await act(async () => document.body.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true })));
    expect(document.querySelector('[role="tooltip"]')).toBeNull();
  });
});
