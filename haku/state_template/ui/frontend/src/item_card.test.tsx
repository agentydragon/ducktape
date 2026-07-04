// @vitest-environment jsdom
import { MantineProvider } from "@mantine/core";
import { cleanup, fireEvent, render as rtlRender, screen } from "@testing-library/react";
import type { ReactElement } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { setResponse } from "./client.ts";
import { ItemCardById } from "./item_card.tsx";
import { repoFile } from "./repo.ts";

vi.mock("./client.ts", () => ({
  setResponse: vi.fn(),
  clearResponse: vi.fn(),
  readResponse: vi.fn().mockResolvedValue(null),
}));
vi.mock("./repo.ts", () => ({ repoFile: vi.fn() }));

window.matchMedia ??= ((query: string) => ({
  matches: false,
  media: query,
  onchange: null,
  addListener: () => {},
  removeListener: () => {},
  addEventListener: () => {},
  removeEventListener: () => {},
  dispatchEvent: () => false,
})) as typeof window.matchMedia;

function render(ui: ReactElement) {
  return rtlRender(<MantineProvider>{ui}</MantineProvider>);
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("ItemCardById", () => {
  it("renders an item by slug and its body's status toggle inherits the card's scope", async () => {
    vi.mocked(setResponse).mockResolvedValue(undefined);
    vi.mocked(repoFile).mockResolvedValue(
      [
        "---",
        "title: Fix the flaky test",
        "value: 70",
        "status: open",
        "---",
        "",
        '<signal-toggle field="status">',
        '<choice value="done">Done</choice>',
        "</signal-toggle>",
      ].join("\n")
    );
    render(<ItemCardById id="fix-the-flaky-test" now={0} />);
    fireEvent.click(await screen.findByText("Fix the flaky test")); // header toggles the body open
    // The toggle carries no scope attribute — it must resolve "fix-the-flaky-test" from the card.
    fireEvent.click(await screen.findByRole("button", { name: "Done" }));
    expect(setResponse).toHaveBeenCalledWith("fix-the-flaky-test", "status", "done");
  });

  it("shows an error when the slug isn't found", async () => {
    vi.mocked(repoFile).mockResolvedValue(null);
    render(<ItemCardById id="ghost" now={0} />);
    expect(await screen.findByText(/item not found: ghost/)).toBeTruthy();
  });
});
