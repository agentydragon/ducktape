// @vitest-environment jsdom
import { MantineProvider } from "@mantine/core";
import { cleanup, render as rtlRender, screen } from "@testing-library/react";
import type { ReactElement } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { InboxView } from "./inbox.tsx";
import type { Doc } from "./repo.ts";
import type { MetaResponse } from "./types.ts";

vi.mock("./bridge.ts", () => ({ openLink: vi.fn() }));
vi.mock("./client.ts", () => ({
  sendFeedback: vi.fn(),
  setResponse: vi.fn(),
  clearResponse: vi.fn(),
  readResponse: vi.fn().mockResolvedValue(null),
}));

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

const meta: MetaResponse = { scan_time: "2026-07-03T00:00:00Z", deployed_commit: null, deployed_commit_url: null };

const docItems: Doc[] = [
  { path: "items/big-thing.md", data: { title: "Big thing", value: 90, status: "open" }, body: "do the big thing" },
  {
    path: "items/small-thing.md",
    data: { title: "Small thing", value: 40, status: "open" },
    body: "do the small thing",
  },
  { path: "items/done-thing.md", data: { title: "Done thing", value: 50, status: "done" }, body: "already done" },
];

describe("InboxView", () => {
  it("renders open items ranked by value; the done item is filtered out of the open list", () => {
    render(<InboxView docItems={docItems} meta={meta} error={null} now={0} />);
    expect(screen.getByText("Big thing")).toBeTruthy();
    expect(screen.getByText("Small thing")).toBeTruthy();
    expect(screen.queryByText("No open items.")).toBeNull();
    // 2 open (done-thing filtered from the open list); status counts cover all three.
    expect(screen.getByText(/2 open/)).toBeTruthy();
    expect(screen.getByText(/done: 1/)).toBeTruthy();
  });

  it("shows a loading state until items arrive, and an error if the read fails", () => {
    const { rerender } = render(<InboxView docItems={null} meta={null} error={null} now={0} />);
    expect(screen.getByText("Loading…")).toBeTruthy();
    rerender(
      <MantineProvider>
        <InboxView docItems={null} meta={null} error="boom" now={0} />
      </MantineProvider>
    );
    expect(screen.getByText(/Failed to load: boom/)).toBeTruthy();
  });
});
