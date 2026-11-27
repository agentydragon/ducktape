import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { lower, prepare, root } from "./harness.mjs";

const entry = "entry.mjs";
const worker = "renderer.mjs";

function placement(moved, retained) {
  return { entry, modules: { [worker]: [moved], [entry]: retained }, order: [worker], sequential: true };
}

const dir = path.join(root, "different-record");
const files = prepare(dir, {
  source: `var read = () => import.meta.env.MODE;
           var meta = () => import.meta;
           export { read, meta };`,
  sourceName: entry,
  provider: "",
  spec: placement("#0", ["#1", "#2"]),
});
const out = path.join(dir, "out");
const { verdict, log } = lower(files.sourceFile, files.specFile, out);
assert.equal(verdict.reason_code, "no_constructed_rule", log);
assert(!existsSync(out));

function instrument(api) {
  const metadata = api.meta();
  assert.equal(api.meta(), metadata);
  let calls = 0;
  let value = {};
  const env = {
    get MODE() {
      assert.equal(this, env);
      return value;
    },
  };
  Object.defineProperty(metadata, "env", {
    get() {
      assert.equal(this, metadata);
      calls++;
      return env;
    },
  });
  return {
    calls: () => calls,
    value: () => value,
    replace: () => {
      value = {};
    },
  };
}

const original = await import(pathToFileURL(files.sourceFile));
const live = instrument(original);
const first = original.read();
assert.equal(first, live.value());
live.replace();
assert.notEqual(first, live.value());
assert.equal(original.read(), live.value());
assert.equal(live.calls(), 2);
