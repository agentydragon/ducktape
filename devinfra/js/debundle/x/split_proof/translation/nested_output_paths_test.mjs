import { strict as assert } from "node:assert";
import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { writeDeployment } from "./testing/deployment_manifest.mjs";
import { dump } from "./parser.mjs";

const temp = process.env.TEST_TMPDIR;
assert(temp, "TEST_TMPDIR is required");
const work = path.join(temp, "nested-output-paths");
mkdirSync(work, { recursive: true });
const code =
  'import { base } from "./dep.mjs";\n' +
  "function add(x) { return x + base; }\n" +
  "const value = add(1);\n" +
  "export { value };\n";
const astPath = path.join(work, "bundle.json");
const specPath = path.join(work, "spec.json");
writeFileSync(astPath, JSON.stringify(dump(code)));
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
const emitted = spawnSync(process.env.LOWERSPLIT, ["--diagnostic", writeDeployment(astPath, specPath, out), out], {
  encoding: "utf8",
});
assert.equal(emitted.status, 0, emitted.stdout + "\n" + emitted.stderr);
assert(existsSync(path.join(out, "public", "entry.mjs")), "the nested entry is written");
assert(existsSync(path.join(out, "lib", "math.mjs")), "a sibling module directory is written");

const entry = readFileSync(path.join(out, "public", "entry.mjs"), "utf8");
const math = readFileSync(path.join(out, "lib", "math.mjs"), "utf8");
assert.match(entry, /import \{\s*add\s*\} from "\.\.\/lib\/math\.mjs";/);
assert.match(math, /import \{\s*base\s*\} from "\.\.\/dep\.mjs";/);
assert.match(entry, /import \{\s*base\s*\} from "\.\.\/dep\.mjs";/);

writeFileSync(path.join(out, "dep.mjs"), "export const base = 42;\n");
const observed = spawnSync(
  process.execPath,
  [
    "--input-type=module",
    "-e",
    "const {value} = await import('./public/entry.mjs'); process.stdout.write(String(value));",
  ],
  { cwd: out, encoding: "utf8" }
);
assert.equal(observed.status, 0, observed.stderr);
assert.equal(observed.stdout, "43", "Node resolves nested glue and retained external relative imports");

const unsafeSpec = path.join(work, "unsafe-spec.json");
const unsafeOut = path.join(work, "unsafe-output");
writeFileSync(
  unsafeSpec,
  JSON.stringify({ entry: "../escape.mjs", modules: { "../escape.mjs": ["#0", "#1", "#2", "#3"] } })
);
const rejected = spawnSync(
  process.env.LOWERSPLIT,
  ["--diagnostic", writeDeployment(astPath, unsafeSpec, unsafeOut), unsafeOut],
  {
    encoding: "utf8",
  }
);
assert.notEqual(rejected.status, 0, "project traversal in a module path is rejected");
assert.match(rejected.stdout + "\n" + rejected.stderr, /unsafe output module path/);
assert(!existsSync(unsafeOut), "an invalid spec does not create its output root");
assert(!existsSync(path.join(work, "escape.mjs")), "an invalid spec cannot write outside its output root");

const closedSource = "const a = 1; const b = 2; export const result = a;\n";
writeFileSync(astPath, JSON.stringify(dump(closedSource)));
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
const certified = spawnSync(
  process.env.LOWERSPLIT,
  [writeDeployment(astPath, specPath, certifiedOut), certifiedOut, "--require", "all-outcomes"],
  {
    encoding: "utf8",
  }
);
const certifiedVerdict = JSON.parse(certified.stdout.trim().split("\n").at(-1));
assert.equal(certified.status, 0, certified.stdout + certified.stderr);
assert.equal(certifiedVerdict.status, "certified", JSON.stringify(certifiedVerdict));
assert.equal(certifiedVerdict.guarantee, "all-outcomes");
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
const wrongTarget = spawnSync(
  process.env.LOWERSPLIT,
  [writeDeployment(astPath, specPath, certifiedOut), certifiedOut, "--require", "all-outcomes", "--check-output"],
  { encoding: "utf8" }
);
const wrongTargetVerdict = JSON.parse(wrongTarget.stdout.trim().split("\n").at(-1));
assert.equal(wrongTarget.status, 1, wrongTarget.stdout + wrongTarget.stderr);
assert.equal(wrongTargetVerdict.status, "rejected", JSON.stringify(wrongTargetVerdict));
assert.match(wrongTargetVerdict.reason, /output module public\/entry\.mjs differs from its generated ESTree/);

// A trailing separator names the same output directory. Publication stages beside it.
const slashOut = path.join(work, "certified-trailing-slash");
const slashed = spawnSync(
  process.env.LOWERSPLIT,
  [writeDeployment(astPath, specPath, slashOut), slashOut + path.sep, "--require", "all-outcomes"],
  { encoding: "utf8" }
);
const slashedVerdict = JSON.parse(slashed.stdout.trim().split("\n").at(-1));
assert.equal(slashed.status, 0, slashed.stdout + slashed.stderr);
assert.equal(slashedVerdict.status, "certified", JSON.stringify(slashedVerdict));
assert.deepEqual(readdirSync(slashOut).sort(), ["other", "parts", "public"]);
assert.deepEqual(
  readdirSync(work).filter((name) => name.startsWith(".split-proof-")),
  [],
  "no staging directory is left behind"
);

// A path ending in .. names no new directory and is refused before certification.
const parentOut = path.join(work, "certified-parent") + path.sep + "..";
const parent = spawnSync(process.env.LOWERSPLIT, [writeDeployment(astPath, specPath, parentOut), parentOut], {
  encoding: "utf8",
});
const parentVerdict = JSON.parse(parent.stdout.trim().split("\n").at(-1));
assert.equal(parent.status, 1, parent.stdout + parent.stderr);
assert.equal(parentVerdict.status, "rejected", JSON.stringify(parentVerdict));
assert.match(parentVerdict.reason, /output path .* must end in a directory name/);
