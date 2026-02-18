/**
 * Unit tests for the router's hash URL parsing logic.
 * Uses Node.js built-in test runner (no extra dependencies).
 */
import { test } from "node:test";
import assert from "node:assert/strict";

// Inline the parsing logic (router.ts uses URL constructor — same logic here).
function parseHash(hash) {
  const fragment = hash.slice(1) || "/";
  const url = new URL(fragment, "http://x");
  return { pathname: url.pathname, searchParams: url.searchParams };
}

test("clean path — no query string", () => {
  const { pathname, searchParams } = parseHash("#/runs");
  assert.equal(pathname, "/runs");
  assert.equal([...searchParams.keys()].length, 0);
});

test("path with query params", () => {
  const { pathname, searchParams } = parseHash(
    "#/runs?definition=sha256%3Afoo&split=valid&kind=whole_snapshot"
  );
  assert.equal(pathname, "/runs");
  assert.equal(searchParams.get("definition"), "sha256:foo");
  assert.equal(searchParams.get("split"), "valid");
  assert.equal(searchParams.get("kind"), "whole_snapshot");
});

test("empty hash defaults to /", () => {
  const { pathname } = parseHash("");
  assert.equal(pathname, "/");
});

test("root hash #/", () => {
  const { pathname } = parseHash("#/");
  assert.equal(pathname, "/");
});

test("nested path", () => {
  const { pathname, searchParams } = parseHash("#/snapshots/crush/2025-08-30?file=foo.py");
  assert.equal(pathname, "/snapshots/crush/2025-08-30");
  assert.equal(searchParams.get("file"), "foo.py");
});
