import { strict as assert } from "node:assert";
import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { split } from "./testing/split.mjs";

const temp = process.env.TEST_TMPDIR;
assert(temp, "TEST_TMPDIR is required");
const work = path.join(temp, "nested-output-paths");
mkdirSync(work, { recursive: true });
const code =
  'import { base } from "./dep.mjs";\n' +
  "function add(x) { return x + base; }\n" +
  "const value = add(1);\n" +
  "export { value };\n";
const bundlePath = path.join(work, "bundle.mjs");
writeFileSync(bundlePath, code);
writeFileSync(path.join(work, "dep.mjs"), "export const base = 42;\n");
const specPath = path.join(work, "spec.json");
writeFileSync(
  specPath,
  JSON.stringify({
    entry: "public/entry.mjs",
    modules: {
      "public/entry.mjs": ["#0", "value", "#3"],
      "lib/math.mjs": ["add"],
    },
  })
);

const out = path.join(work, "generated");
assert(!existsSync(out), "the output root starts absent");
// The constructor does not yet support a retained external relative import.
const external = split({ source: bundlePath, spec: specPath, out });
assert.equal(external.status, "refused", external.log);
assert.equal(external.reasonCode, "no_constructed_rule", external.log);
assert(!existsSync(out));

const unsafeSpec = path.join(work, "unsafe-spec.json");
const unsafeOut = path.join(work, "unsafe-output");
writeFileSync(
  unsafeSpec,
  JSON.stringify({ entry: "../escape.mjs", modules: { "../escape.mjs": ["#0", "#1", "#2", "#3"] } })
);
const rejected = split({ source: bundlePath, spec: unsafeSpec, out: unsafeOut });
assert.equal(rejected.status, "refused", rejected.log);
assert.match(rejected.reason, /unsafe output module path/);
assert(!existsSync(unsafeOut), "an invalid spec does not create its output root");
assert(!existsSync(path.join(work, "escape.mjs")), "an invalid spec cannot write outside its output root");

const closedSource = "const a = 1; const b = 2; export const result = a;\n";
writeFileSync(bundlePath, closedSource);
writeFileSync(
  specPath,
  JSON.stringify({
    entry: "public/entry.mjs",
    order: ["parts/left.mjs", "other/right.mjs"],
    sequential: true,
    modules: {
      "public/entry.mjs": ["#2"],
      "parts/left.mjs": ["#0"],
      "other/right.mjs": ["#1"],
    },
  })
);
const certifiedOut = path.join(work, "certified-nested");
const certified = split({ source: bundlePath, spec: specPath, out: certifiedOut, require: "all-outcomes" });
assert.equal(certified.status, "certified", certified.log);
assert.equal(certified.guarantee, "all-outcomes");
assert.equal(
  spawnSync(
    process.execPath,
    [
      "--input-type=module",
      "-e",
      "const {result} = await import('./public/entry.mjs'); process.stdout.write(String(result));",
    ],
    { cwd: certifiedOut, encoding: "utf8" }
  ).stdout,
  "1"
);

const certifiedEntry = path.join(certifiedOut, "public", "entry.mjs");
const certifiedText = readFileSync(certifiedEntry, "utf8");
assert.match(certifiedText, /^import "\.\.\/other\/right\.mjs";$/m);
const expectedTarget = 'from "../parts/left.mjs"';
assert(certifiedText.includes(expectedTarget), certifiedText);
writeFileSync(certifiedEntry, certifiedText.replace(expectedTarget, 'from "../other/right.mjs"'));
const wrongTarget = split({
  source: bundlePath,
  spec: specPath,
  out: certifiedOut,
  require: "all-outcomes",
  checkOutput: true,
});
assert.equal(wrongTarget.status, "refused", wrongTarget.log);
assert.match(wrongTarget.reason, /output module public\/entry\.mjs differs from its generated ESTree/);

// A trailing separator names the same output directory. Publication stages beside it.
const slashOut = path.join(work, "certified-trailing-slash");
const slashed = split({ source: bundlePath, spec: specPath, out: slashOut + path.sep, require: "all-outcomes" });
assert.equal(slashed.status, "certified", slashed.log);
assert.deepEqual(readdirSync(slashOut).sort(), ["other", "parts", "public"]);
assert.deepEqual(
  readdirSync(work).filter((name) => name.startsWith(".split-proof-")),
  [],
  "no staging directory is left behind"
);

// A path ending in .. names no new directory and is refused before certification.
const parentOut = path.join(work, "certified-parent") + path.sep + "..";
const parent = split({ source: bundlePath, spec: specPath, out: parentOut, allowExistingOut: true });
assert.equal(parent.status, "refused", parent.log);
assert.match(parent.reason, /output path .* must end in a directory name/);
