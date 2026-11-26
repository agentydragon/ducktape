import assert from "node:assert/strict";
import { existsSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { forcePreview, lower, prepare, root } from "./harness.mjs";

const entry = "entry.mjs";
const worker = "renderer.mjs";

function placement(moved, retained) {
  return { entry, modules: { [worker]: [moved], [entry]: retained }, order: [worker], sequential: true };
}

// Move only the independent renderer. Both metadata reads stay at the entry URL.
for (const member of ["url", "env.MODE"]) {
  const dir = path.join(root, "retained-" + member);
  const { sourceFile, specFile } = prepare(dir, {
    source: `var read = () => import.meta.${member}; var render = () => 42; export { read, render };`,
    sourceName: entry,
    provider: "",
    spec: placement("#1", ["#0", "#2"]),
  });
  const out = path.join(dir, "out");
  const profile = path.join(dir, "host.json");
  writeFileSync(profile, JSON.stringify({ profile: "browser-page" }));
  const options = ["--structural", "--deployment-origin", "https://example.org", "--host-contract", profile];
  if (member === "url") {
    const { verdict } = lower(sourceFile, specFile, out, options);
    assert.equal(verdict.status, "rejected", JSON.stringify(verdict));
    assert.equal(verdict.reason_code, "strict_observation_unsupported", JSON.stringify(verdict));
    assert(!existsSync(out));
  } else {
    const { verdict } = lower(sourceFile, specFile, out, options, "linked-structural");
    assert.equal(verdict.status, "rejected", JSON.stringify(verdict));
    assert.match(verdict.reason, /unit 0: expected import.meta.url/);
    assert(!existsSync(out));
    const preset = [...options, "--deployment-preset", "native-browser-record@1"];
    const presetRefusal = lower(sourceFile, specFile, out, preset).verdict;
    assert.equal(presetRefusal.status, "rejected", JSON.stringify(presetRefusal));
    assert.equal(presetRefusal.reason_code, "strict_observation_unsupported", JSON.stringify(presetRefusal));
    assert(!existsSync(out));
  }
}

// One native module record shares mutable metadata between its closures. The outer
// getter returns an object; the inner getter can see a changed cell on a later call.
const dir = path.join(root, "different-record");
const files = prepare(dir, {
  source: `var read = () => import.meta.env.MODE;
           var meta = () => import.meta;
           export { read, meta };`,
  sourceName: entry,
  provider: "",
  spec: placement("#0", ["#1", "#2"]),
});
const out = path.join(dir, "preview");
forcePreview(files, out);

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

assert.deepEqual(Object.keys(original), ["meta", "read"]);

const profile = path.join(root, "browser-profile.json");
writeFileSync(profile, JSON.stringify({ profile: "browser-page" }));
const preset = [
  "--structural",
  "--deployment-origin",
  "https://example.org",
  "--host-contract",
  profile,
  "--deployment-preset",
  "native-browser-record@1",
];

// Relocating the reader changes its module record even if the initializer is identical.
const changed = lower(files.sourceFile, files.specFile, path.join(dir, "checked"), preset, "linked-structural").verdict;
assert.match(changed.reason, /URL-sensitive unit 0 moved/);

// Native creation is complete before user code installs this getter. It now
// reaches pivot's live cell; moving just its initializer changes the observation.
const post = path.join(root, "post-init-getter");
const later = prepare(post, {
  source: `Object.defineProperty(import.meta, "env", { get() { return { MODE: pivot }; } });
           var read = () => import.meta.env.MODE;
           var observed = read();
           var pivot = "ready";
           export {observed, pivot};`,
  sourceName: entry,
  provider: "",
  spec: placement("#3", ["#0", "#1", "#2", "#4"]),
});
const refused = lower(
  later.sourceFile,
  later.specFile,
  path.join(post, "checked"),
  preset,
  "linked-structural"
).verdict;
assert.equal(refused.status, "rejected");
assert.match(refused.reason, /unit 0 has access to the moved region of unit 3/);
const forced = path.join(post, "preview");
forcePreview(later, forced);
assert.equal((await import(pathToFileURL(later.sourceFile))).observed, undefined);
assert.equal((await import(pathToFileURL(path.join(forced, entry)))).observed, "ready");
