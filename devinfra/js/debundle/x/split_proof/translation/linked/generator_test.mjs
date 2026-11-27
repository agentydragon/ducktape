import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { runSplit } from "./harness.mjs";

// The CLI cannot construct these placements, including the move that would
// change which value a generator sees on its first resume.
for (const [name, source, worker, entry, expected] of [
  ["declaration", "function* run() { yield 1; } export const before = run().next().value;", [0], [1], 1],
  ["method", "const box = { *run() { yield 2; } }; export const before = box.run().next().value;", [0], [1], 2],
  ["async", "async function* run() { yield 3; } export const before = (await run().next()).value;", [0], [1], 3],
  [
    "before-initialization",
    "function* read() { yield later; } const before = read().next().value; var later = 7; export { before };",
    [2],
    [0, 1, 3],
    undefined,
  ],
]) {
  const test = runSplit(name, {
    source,
    provider: "",
    spec: {
      entry: "entry.mjs",
      modules: { "worker.mjs": worker.map((n) => "#" + n), "entry.mjs": entry.map((n) => "#" + n) },
      order: ["worker.mjs"],
      sequential: true,
    },
  });
  assert.equal((await import(pathToFileURL(test.files.sourceFile))).before, expected);
  assert.equal(test.verdict.status, "rejected", test.log);
  assert.equal(test.verdict.reason_code, "no_constructed_rule", test.log);
  assert(!existsSync(test.out));
}
