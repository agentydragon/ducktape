// @vitest-environment jsdom
import { MantineProvider } from "@mantine/core";
import { cleanup, fireEvent, render as rtlRender, screen, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { sendFeedback } from "./client.ts";
import { NoteToHaku } from "./feedback_button.tsx";

vi.mock("./client.ts", () => ({ sendFeedback: vi.fn().mockResolvedValue(undefined) }));

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

// Mantine's Modal (Portal + FocusTrap + ScrollArea) needs ResizeObserver, which jsdom lacks.
window.ResizeObserver ??= class {
  observe() {}
  unobserve() {}
  disconnect() {}
};

// The autosize Textarea subscribes to document.fonts (font-load reflow), absent in jsdom.
Object.defineProperty(document, "fonts", {
  configurable: true,
  value: { addEventListener() {}, removeEventListener() {} },
});

function render(ui: ReactElement) {
  return rtlRender(<MantineProvider>{ui}</MantineProvider>);
}

// Snapshot a selection at the moment the button is clicked, then make it return "" (focusing the
// textarea would clear it in a real browser) — proves NoteToHaku captures at click time, not later.
function selectionThatClearsAfterFirstRead(first: string) {
  let reads = 0;
  vi.spyOn(window, "getSelection").mockImplementation(
    () => ({ toString: () => (reads++ === 0 ? first : "") }) as Selection
  );
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks(); // restores the window.getSelection spy each test creates
});

describe("NoteToHaku", () => {
  it("captures the current page hash and the selected text at click time and shows them in the modal", async () => {
    window.location.hash = "#/runs";
    selectionThatClearsAfterFirstRead("6 scanned · 2 skipped");
    render(<NoteToHaku />);
    fireEvent.click(screen.getByRole("button", { name: /Note/ }));
    // 21 chars selected, reported from #/runs — the transparency line the operator sees.
    expect(await screen.findByText(/Reporting from #\/runs · 21 chars selected/)).toBeTruthy();
  });

  it("sends the captured page + selection alongside the note text", async () => {
    vi.mocked(sendFeedback).mockResolvedValue(undefined);
    window.location.hash = "#/runs";
    selectionThatClearsAfterFirstRead("hello selection");
    render(<NoteToHaku />);
    fireEvent.click(screen.getByRole("button", { name: /Note/ }));
    fireEvent.change(await screen.findByPlaceholderText(/Anything for Haku/), {
      target: { value: "this page looks bad" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Send to Haku/ }));
    await waitFor(() =>
      expect(sendFeedback).toHaveBeenCalledWith("this page looks bad", undefined, {
        page: "#/runs",
        selection: "hello selection",
      })
    );
  });

  it("reports the page with a null selection when nothing is selected", async () => {
    window.location.hash = "#/inbox";
    selectionThatClearsAfterFirstRead("");
    render(<NoteToHaku />);
    fireEvent.click(screen.getByRole("button", { name: /Note/ }));
    expect(await screen.findByText(/Reporting from #\/inbox/)).toBeTruthy();
    expect(screen.queryByText(/chars selected/)).toBeNull();
  });
});
