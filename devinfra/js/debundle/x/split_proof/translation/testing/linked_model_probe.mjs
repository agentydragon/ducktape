import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { readFileSync, readdirSync } from "node:fs";
import path from "node:path";
import { dump } from "../parser.mjs";

const parse = (text) => dump(text.toString("utf8"), { includeOpaqueCaptures: true });

/** Probe the original and actual preview in CoreJS. All paths name test files. */
export function probeLinkedModel({ source, spec, directory, observe, host, providers = [] }) {
  assert(process.env.LINKED_MODEL_CHECKER, "LINKED_MODEL_CHECKER is required");
  const shared = Object.create(null);
  for (const name of providers) {
    assert(!path.isAbsolute(name) && !name.split("/").some((part) => ["", ".", ".."].includes(part)), name);
    const from = readFileSync(path.join(path.dirname(source), name));
    assert.deepEqual(readFileSync(path.join(directory, name)), from, `fixed provider differs: ${name}`);
    shared[name] = parse(from);
  }

  const files = Object.create(null);
  for (const entry of readdirSync(directory, { recursive: true, withFileTypes: true })) {
    if (!entry.isFile() || !/\.m?js$/.test(entry.name)) continue;
    const file = path.join(entry.parentPath, entry.name);
    const name = path.relative(directory, file);
    if (!Object.hasOwn(shared, name)) files[name] = parse(readFileSync(file));
  }

  const result = spawnSync(process.env.LINKED_MODEL_CHECKER, [], {
    encoding: "utf8",
    input: JSON.stringify({
      source: parse(readFileSync(source)),
      spec: readFileSync(spec, "utf8"),
      files,
      providers: shared,
      observe,
      host,
    }),
  });
  if (result.error) throw result.error;
  const log = result.stdout + result.stderr;
  assert([0, 1].includes(result.status), log);
  const report = JSON.parse(result.stdout);
  assert.equal(report.status, result.status === 0 ? "matched" : "refused", log);
  return report;
}
