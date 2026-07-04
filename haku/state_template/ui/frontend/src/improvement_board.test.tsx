// @vitest-environment jsdom
import { MantineProvider } from "@mantine/core";
import { cleanup, render as rtlRender, screen } from "@testing-library/react";
import type { ReactElement } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ImprovementBoard } from "./improvement_board.tsx";

// jsdom has no matchMedia; MantineProvider's color-scheme detection needs it.
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

const TREE = {
  sha: "h",
  entries: [
    { path: "memory/improvements/alpha.md", type: "blob", sha: "a1" },
    { path: "memory/improvements/bravo.md", type: "blob", sha: "b2" },
    { path: "memory/improvements/junk.md", type: "blob", sha: "c3" }, // wrong kind → skipped
  ],
};
const BLOBS: Record<string, string> = {
  a1: "---\nkind: improvement\nclass: idea\ntitle: Alpha idea\nweight: high\nstatus: recommend\nsummary: do alpha\n---\nalpha detail",
  b2: "---\nkind: improvement\nclass: friction\ntitle: Bravo friction\nweight: medium\nstatus: open\n---\nbravo detail",
  c3: "---\nkind: note\ntitle: Junk\n---\nnope",
};

function stubFetch() {
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string) => {
      if (url === "/api/repo/tree") {
        return Promise.resolve({ ok: true, json: () => Promise.resolve(TREE) } as Response);
      }
      const shas = new URL(url, "http://x").searchParams.get("shas")!.split(",");
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve(shas.map((s) => ({ sha: s, content: BLOBS[s] }))),
      } as Response);
    })
  );
}

describe("ImprovementBoard", () => {
  // Unmount between tests (vitest globals are off, so RTL's auto-cleanup isn't registered);
  // otherwise the async fetch's late setState fires after the jsdom env is torn down.
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("renders ideas and friction from the collection, splitting by class", async () => {
    stubFetch();
    render(<ImprovementBoard />);
    // async fetch → wait for the first card
    expect(await screen.findByText("Alpha idea")).toBeTruthy();
    expect(screen.getByText("Bravo friction")).toBeTruthy();
    expect(screen.getByText("do alpha")).toBeTruthy(); // idea summary
    // status + weight badges prove class-specific rendering
    expect(screen.getByText("recommend")).toBeTruthy();
    expect(screen.getByText("open")).toBeTruthy();
    expect(screen.getByText("high value")).toBeTruthy(); // idea → "<weight> value"
    expect(screen.getByText("medium")).toBeTruthy(); // friction → bare weight
  });

  it("skips files whose frontmatter kind isn't `improvement` (never crashes)", async () => {
    stubFetch();
    render(<ImprovementBoard />);
    await screen.findByText("Alpha idea");
    expect(screen.queryByText("Junk")).toBeNull();
  });
});
