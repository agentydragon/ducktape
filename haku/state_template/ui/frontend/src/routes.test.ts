import { describe, expect, it } from "vitest";

import { formatHash, HOME, parseHash, type Route } from "./routes.ts";

describe("parseHash", () => {
  it("defaults to Inbox for empty/bare hashes", () => {
    for (const h of ["", "#", "#/"]) expect(parseHash(h)).toEqual(HOME);
  });

  it("falls back to Inbox on unknown routes instead of crashing", () => {
    expect(parseHash("#/nonsense")).toEqual(HOME);
    expect(parseHash("#/https://evil.example")).toEqual(HOME);
  });

  it("parses plain views", () => {
    expect(parseHash("#/runs")).toEqual({ view: "runs", gardenPath: null });
    expect(parseHash("#/improvements")).toEqual({ view: "improvements", gardenPath: null });
  });

  it("treats a trailing slash as the garden index, not an empty file", () => {
    expect(parseHash("#/garden/")).toEqual({ view: "garden", gardenPath: null });
  });

  it("falls back to Inbox on malformed percent-encoding instead of throwing", () => {
    expect(parseHash("#/garden/%E0%A4%A")).toEqual(HOME);
  });

  it("parses the garden index vs a garden file", () => {
    expect(parseHash("#/garden")).toEqual({ view: "garden", gardenPath: null });
    expect(parseHash("#/garden/memory/operator.md")).toEqual({
      view: "garden",
      gardenPath: "memory/operator.md",
    });
  });

  it("decodes encoded path segments", () => {
    expect(parseHash("#/garden/runs/2026-07-01/a%20b.md").gardenPath).toBe("runs/2026-07-01/a b.md");
  });
});

describe("formatHash / round-trip", () => {
  it("formats Inbox as the bare root", () => {
    expect(formatHash(HOME)).toBe("#/");
  });

  it("keeps slashes readable in garden paths", () => {
    expect(formatHash({ view: "garden", gardenPath: "memory/operator.md" })).toBe("#/garden/memory/operator.md");
  });

  it("round-trips every route shape", () => {
    const routes: Route[] = [
      HOME,
      { view: "improvements", gardenPath: null },
      { view: "runs", gardenPath: null },
      { view: "garden", gardenPath: null },
      { view: "garden", gardenPath: "procedures/tana_runbook.md" },
      { view: "garden", gardenPath: "runs/2026-07-01/note with spaces.md" },
    ];
    for (const r of routes) expect(parseHash(formatHash(r))).toEqual(r);
  });
});
