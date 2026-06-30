import { describe, expect, it } from "vitest";

import { isExternal, resolveRepoPath } from "./mdx.tsx";

// resolveRepoPath turns a markdown link (relative or repo-absolute) into a repo path, so garden
// notes can link each other — the load-bearing bit of "keep it interlinked". Edge cases: `.`/`..`
// segments, a leading `/` meaning repo-root, a missing base, and stripped fragments/queries.
describe("resolveRepoPath", () => {
  it("resolves a bare sibling against the file's directory", () => {
    expect(resolveRepoPath("memory/a/b.md", "c.md")).toBe("memory/a/c.md");
  });
  it("resolves an explicit ./ sibling", () => {
    expect(resolveRepoPath("memory/a/b.md", "./c.md")).toBe("memory/a/c.md");
  });
  it("walks up with ..", () => {
    expect(resolveRepoPath("memory/a/b.md", "../c.md")).toBe("memory/c.md");
    expect(resolveRepoPath("runs/2026-06-30/x.md", "../../procedures/p.md")).toBe("procedures/p.md");
  });
  it("treats a leading / as repo root (ignores the base dir)", () => {
    expect(resolveRepoPath("memory/a/b.md", "/runs/x.md")).toBe("runs/x.md");
  });
  it("resolves against repo root when there is no base", () => {
    expect(resolveRepoPath(undefined, "memory/x.md")).toBe("memory/x.md");
  });
  it("strips a #fragment / ?query before resolving", () => {
    expect(resolveRepoPath("memory/a.md", "b.md#section")).toBe("memory/b.md");
    expect(resolveRepoPath("memory/a.md", "b.md?v=1")).toBe("memory/b.md");
  });
  it("does not climb above the repo root", () => {
    expect(resolveRepoPath("a.md", "../../x.md")).toBe("x.md");
  });
});

describe("isExternal", () => {
  it("is true for URL-schemed and protocol-relative links", () => {
    for (const href of ["https://x.com", "http://x", "mailto:a@b.com", "//cdn.example/x"]) {
      expect(isExternal(href)).toBe(true);
    }
  });
  it("is false for repo-relative links and in-page anchors", () => {
    for (const href of ["memory/x.md", "../x.md", "/runs/x.md", "#anchor", ""]) {
      expect(isExternal(href)).toBe(false);
    }
  });
});
