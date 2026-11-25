import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { copyFileSync, mkdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { dump } from "../parser.mjs";

const fixture = path.join(path.dirname(fileURLToPath(import.meta.url)), "fixtures/static_provider_boundary");
const text = (name) => readFileSync(path.join(fixture, name), "utf8");
const parse = (name) => dump(text(name), { includeOpaqueCaptures: true });
const source = parse("source/entry.mjs");
const providerInterface = parse("provider-interface.mjs");
const part = parse("output/part.mjs");

function inspect(entry, providers = { "provider.mjs": providerInterface }) {
  const input = { source, providers, files: { "entry.mjs": parse(entry), "part.mjs": part } };
  const result = spawnSync(process.env.PROVIDER_BOUNDARY_CHECKER, [], {
    encoding: "utf8",
    input: JSON.stringify(input) + "\n",
  });
  assert.ifError(result.error);
  assert.equal(result.status, 0, result.stderr);
  return JSON.parse(result.stdout);
}

const positive = inspect("output/entry.mjs");
assert.equal(positive.status, "ok", JSON.stringify(positive));
assert.deepEqual(positive.result.entry.map(([name]) => name).sort(), ["live", "snapshot"]);
assert.equal(positive.result.worldTurns, 6);
const miswired = inspect("wrong/entry.mjs");
assert.equal(miswired.status, "refused", JSON.stringify(miswired));
assert.match(miswired.reason, /entry requests for providers differ/);

// The decoder currently requires an ESTree-shaped provider interface.
const absent = inspect("output/entry.mjs", {
  "provider.mjs": { type: "ProviderInterface", exports: ["value", "alternate", "set"] },
});
assert.equal(absent.status, "refused", JSON.stringify(absent));
assert.equal(absent.reason, "provider.mjs: expected an ESTree Program");

async function runtime(name, entry, includePart) {
  const dir = path.join(process.env.TEST_TMPDIR, name);
  mkdirSync(dir);
  copyFileSync(path.join(fixture, entry), path.join(dir, "entry.mjs"));
  copyFileSync(path.join(fixture, "provider-runtime.mjs"), path.join(dir, "provider.mjs"));
  if (includePart) copyFileSync(path.join(fixture, "output/part.mjs"), path.join(dir, "part.mjs"));
  const app = await import(pathToFileURL(path.join(dir, "entry.mjs")));
  const provider = await import(pathToFileURL(path.join(dir, "provider.mjs")));
  const initial = [app.live, app.snapshot, provider.value, provider.writes];
  provider.set(false);
  const afterFalse = [app.live, app.snapshot, provider.value, provider.writes];
  provider.set(true);
  return { initial, afterFalse, afterTrue: [app.live, app.snapshot, provider.value, provider.writes] };
}

const expected = {
  initial: [true, true, true, 1],
  afterFalse: [false, true, false, 2],
  afterTrue: [true, true, true, 3],
};
assert.deepEqual(await runtime("source", "source/entry.mjs", false), expected);
assert.deepEqual(await runtime("output", "output/entry.mjs", true), expected);
assert.deepEqual(await runtime("wrong", "wrong/entry.mjs", true), {
  initial: [false, false, true, 1],
  afterFalse: [false, false, false, 2],
  afterTrue: [false, false, true, 3],
});
