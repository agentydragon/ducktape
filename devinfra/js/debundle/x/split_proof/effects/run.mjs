// Runs every case of `cases.mjs` and `cases_general.mjs`: front end, `lower`, and compares each unit's classification with the expectation.
// A case may set its own `premises` (those of its file otherwise) and list `reorders`: the same units in another order,
// written as a one-module tree. Node runs both; the
// order must change what Node prints (`differs`) or not (`same`), and `translate` must not accept a tree that
// differs. A `same` reorder with `accepted: true` is a positive anchor: the analysis is precise enough to allow it.
//
//   node run.mjs [--show] [--permutations N | --known-gaps | --generated-known-gaps] [--seed S]
//
// `--permutations N` also runs N random permutations of the units of every case (after the listed reorders) and fails on
// a wrong accept: a tree that `translate` accepts and Node runs differently.
import { spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const lower = process.env.LOWER ?? path.join(here, "../lean/.lake/build/bin/lower");
const translate = process.env.TRANSLATE ?? path.join(here, "../lean/.lake/build/bin/translate");
const pair = path.join(here, "../translation/pair.mjs");
const frontend = path.join(here, "../translation/frontend.mjs");
const show = process.argv.includes("--show");
const option = (name, fallback) => {
  const i = process.argv.lastIndexOf(`--${name}`);
  return i < 0 ? fallback : Number(process.argv[i + 1]);
};
const permutations = option("permutations", 0);
const caseSeed = option("seed", 1);
const auditGaps = process.argv.includes("--known-gaps");
const generatedGaps = process.argv.includes("--generated-known-gaps");
let seed = caseSeed;

// `CASES=path` runs another file of cases with the same shape; a relative path is from this script's directory. Without
// it, `node run.mjs` takes `cases.mjs` and `cases_general.mjs` beside it. A generated module may export
// `generateCases(seed)` and `requiredFamilies`. A case without `premises` of its own gets those of its file.
const knownGaps = {
  "./debug/general_known_gaps/cases.mjs": "intrinsic summaries",
  "./debug/p1_known_gaps/cases.mjs": "P1",
  "../translation/debug/generator_findings/cases.mjs": "P3",
};
if ((auditGaps || generatedGaps) && (process.env.CASES || permutations || (auditGaps && generatedGaps)))
  throw new Error("select one known-gap audit and use its own cases and reorders");
const caseFiles = auditGaps
  ? Object.keys(knownGaps)
  : generatedGaps
    ? ["../translation/gen/implicit_calls.mjs"]
    : process.env.CASES
      ? [process.env.CASES]
      : ["./cases.mjs", "./cases_general.mjs"];
const modules = await Promise.all(caseFiles.map((f) => import(pathToFileURL(path.resolve(here, f)).href)));
const cases = modules.flatMap((m, i) => {
  const generated = m.generateCases ? m.generateCases(caseSeed) : m.cases;
  return generated.map((c) => ({
    ...c,
    premises: c.premises ?? m.premises,
    knownGap: auditGaps ? knownGaps[caseFiles[i]] : undefined,
  }));
});
const requiredFamilies = Object.assign({}, ...modules.map((m) => m.requiredFamilies ?? {}));
const familyCounts = new Map();
for (const c of cases) if (c.family) familyCounts.set(c.family, (familyCounts.get(c.family) ?? 0) + 1);
for (const [family, minimum] of Object.entries(requiredFamilies)) {
  if ((familyCounts.get(family) ?? 0) < minimum) throw new Error(`generator emitted too few ${family} cases`);
}
// mulberry32: a small seeded generator, so a failing permutation can be run again
const random = () => {
  seed = (seed + 0x6d2b79f5) >>> 0;
  let t = Math.imul(seed ^ (seed >>> 15), seed | 1);
  t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
  return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
};
const shuffled = (n) => {
  const a = [...Array(n).keys()];
  for (let i = n - 1; i > 0; i--) {
    const j = Math.floor(random() * (i + 1));
    [a[i], a[j]] = [a[j], a[i]];
  }
  return a;
};
const work = mkdtempSync(path.join(tmpdir(), "effects_"));
const premisesOf = (c, n) => {
  const file = path.join(work, `premises_${n}.json`);
  writeFileSync(file, JSON.stringify(c.premises));
  return file;
};

// `world` says nothing about what the unit may change; a case that expects `world writes a b` (`world writes -` for no
// names) also checks that.
const classify = (f, wanted) => {
  const own = new Set(f.defs);
  const others = f.writes.filter((n) => !own.has(n)).sort();
  if (f.effect) return wanted?.startsWith("world writes ") ? `world writes ${others.join(" ") || "-"}` : "world";
  return others.length === 0 ? "pure" : `writes ${others.join(" ")}`;
};
const normalize = (e) => {
  const m = e.match(/^(world )?writes (.*)$/);
  return m ? `${m[1] ?? ""}writes ${m[2].split(" ").sort().join(" ")}` : e;
};

// A module outside the tree that a case imports: state that the program shares with the world.
const vendorFile = path.join(work, "vendor_module.js");
writeFileSync(
  vendorFile,
  [
    "export const lib = { settings: { debug: false } };",
    // a descriptor whose getter calls back a function the program left in a global
    "export const mk = () => ({ get() { return globalThis.__cb && globalThis.__cb(); }, configurable: true });",
    // a function that gives its argument a conversion hook, which calls back the same function
    'export const hook = (o) => { o.toString = () => { globalThis.__cb && globalThis.__cb(); return ""; }; };',
    // a binding that the module's own code changes
    "export let count = 0;",
    "export const bump = () => { count += 1; };",
    "",
  ].join("\n")
);

const nodeOutput = (file) => {
  const r = spawnSync(process.execPath, [file], { encoding: "utf8", timeout: 20000 });
  if (r.error || r.signal) throw new Error(`Node execution failed for ${file}: ${r.error ?? r.signal}`);
  return { status: r.status, stdout: r.stdout, firstError: r.status === 0 ? "" : r.stderr.split("\n")[0] };
};
const sameNodeOutput = (left, right) =>
  left.status === right.status && left.stdout === right.stdout && left.firstError === right.firstError;

// The verdict of `translate` on the bundle's units in `order`, all in one module, and Node's verdict.
const reorder = (c, bundleJson, lines, order) => {
  const out = path.join(work, "out");
  rmSync(out, { recursive: true, force: true });
  mkdirSync(out);
  writeFileSync(path.join(out, "package.json"), '{"type":"module"}');
  writeFileSync(path.join(out, "entry.js"), order.map((i) => lines[i]).join("\n") + "\n");
  const outputJson = path.join(work, "output.json");
  const certJson = path.join(work, "cert.json");
  for (const [name, args] of [
    ["tree", [frontend, "tree", out, "--entry", "entry.js", "--out", outputJson]],
    ["pairing", [pair, bundleJson, outputJson, "--out", certJson]],
  ]) {
    const r = spawnSync(process.execPath, args, { encoding: "utf8" });
    if (r.status !== 0) throw new Error(`${c.name}: ${name} of [${order}]: ${r.stderr}`);
  }
  const checked = spawnSync(translate, [bundleJson, outputJson, certJson, "--premises", c.premisesFile], {
    encoding: "utf8",
    maxBuffer: 1 << 26,
  });
  const verdict = { 0: "accepted", 1: "rejected", 4: "obligations" }[checked.status];
  if (!verdict || (verdict === "accepted" && !/^PLAN SOUND$/m.test(checked.stdout)))
    throw new Error(`${c.name}: translate exit ${checked.status}: ${checked.stdout}${checked.stderr}`);
  return { verdict, node: nodeOutput(path.join(out, "entry.js")) };
};

let failures = 0;
const observedFamilies = new Set();
const observedGaps = new Map();
const observedGapFamilies = new Map();
for (const [n, raw] of cases.entries()) {
  const c = { ...raw, source: raw.source.replaceAll("%VENDOR%", vendorFile), premisesFile: premisesOf(raw, n) };
  const source = path.join(work, "case.mjs");
  const bundle = path.join(work, "bundle.json");
  writeFileSync(source, c.source);
  const front = spawnSync(process.execPath, [frontend, "bundle", source, "--out", bundle], { encoding: "utf8" });
  if (front.status !== 0) throw new Error(`${c.name}: ${front.stderr}`);
  const run = spawnSync(lower, [bundle, "--premises", c.premisesFile], { encoding: "utf8", maxBuffer: 1 << 26 });
  if (run.status !== 0) throw new Error(`${c.name}: ${run.stderr}`);
  const footprints = run.stdout
    .trim()
    .split("\n")
    .map((l) => JSON.parse(l));
  const want = c.expect?.map(normalize);
  const got = footprints.map((f, i) => classify(f, want?.[i]));
  const ok = want === undefined || (got.length === want.length && got.every((g, i) => g === want[i]));
  if (!ok) failures++;
  console.log(`${ok ? "pass" : "FAIL"}  ${c.name}`);
  const lines = c.source.trim().split("\n");
  const original = c.reorders?.length || permutations ? nodeOutput(source) : undefined;
  for (const r of c.reorders ?? []) {
    const gap = c.knownGap ?? (generatedGaps ? r.knownGap : undefined);
    const res = reorder(c, bundle, lines, r.order);
    const expectedWrongAccept = gap && res.verdict === "accepted";
    const differs = !sameNodeOutput(res.node, original);
    const problems = [];
    if (differs !== (r.behavior === "differs"))
      problems.push(`Node ${differs ? "differs" : "agrees"}, the case says ${r.behavior}`);
    if (r.stdoutDiffers && res.node.stdout === original.stdout) problems.push("Node stdout did not differ");
    if (gap) {
      if (original.status !== 0 || res.node.status !== 0 || original.stdout === res.node.stdout)
        problems.push("known gap requires both Node runs to complete and print different output");
      if (c.knownGap && !expectedWrongAccept)
        problems.push(`known gap no longer accepted (${res.verdict}); move to a regression test`);
      if (gap === "P3") {
        const defaultPremises = premisesOf({ premises: { ...c.premises, noWorldHooks: false } }, n + cases.length);
        const control = reorder({ ...c, premisesFile: defaultPremises }, bundle, lines, r.order);
        if (control.verdict === "accepted") problems.push("WRONG ACCEPT with P3 off");
      }
    } else if (differs && res.verdict === "accepted") problems.push("WRONG ACCEPT");
    if (r.accepted && res.verdict !== "accepted") problems.push(`wanted accepted, got ${res.verdict}`);
    if (problems.length) failures++;
    else if (expectedWrongAccept) {
      observedGaps.set(gap, (observedGaps.get(gap) ?? 0) + 1);
      if (c.family) observedGapFamilies.set(c.family, (observedGapFamilies.get(c.family) ?? 0) + 1);
    }
    if (differs && c.family) observedFamilies.add(c.family);
    console.log(
      `  ${problems.length ? "FAIL" : expectedWrongAccept ? `XFAIL (${gap})` : "pass"}  [${r.order}] ${res.verdict}; Node ${differs ? "differs" : "agrees"}${problems.length ? ` (${problems.join("; ")})` : ""}`
    );
  }
  // a bundle that throws in Node is outside premise P4: its permutations would differ by the error's file and line
  if (permutations && original.status !== 0) console.log("  skip  permutations: the case throws in Node");
  else if (permutations) {
    const tally = { accepted: 0, obligations: 0, rejected: 0 };
    const wrong = [];
    for (let k = 0; k < permutations; k++) {
      const order = shuffled(lines.length);
      const res = reorder(c, bundle, lines, order);
      tally[res.verdict]++;
      if (res.verdict === "accepted" && !sameNodeOutput(res.node, original)) wrong.push(order);
    }
    if (wrong.length) failures++;
    console.log(
      `  ${wrong.length ? "FAIL" : "pass"}  ${permutations} permutations: ${tally.accepted} accepted, ${tally.obligations} obligations, ${tally.rejected} rejected${wrong.length ? `; WRONG ACCEPT [${wrong.join("] [")}]` : ""}`
    );
  }
  if (!ok || show) {
    const lines = c.source
      .trim()
      .split("\n")
      .filter((l) => !l.startsWith("import "));
    // one source line per unit is the shape of every case; show the two side by side
    lines.forEach((l, i) =>
      console.log(`    ${(got[i] ?? "-").padEnd(28)} ${!ok && got[i] !== want[i] ? `(wanted ${want[i]}) ` : ""}${l}`)
    );
  }
}
rmSync(work, { recursive: true, force: true });
const missingFamilies = Object.keys(requiredFamilies).filter((family) => !observedFamilies.has(family));
if (failures || missingFamilies.length) {
  const missing = missingFamilies.length ? `; no observable differing reorder for: ${missingFamilies.join(", ")}` : "";
  throw new Error(`${failures} cases gave the wrong classification${missing}`);
}
for (const [category, count] of observedGaps) console.log(`${count} expected wrong accepts (${category})`);
if (generatedGaps) {
  for (const [family, count] of observedGapFamilies)
    console.log(`${family}: ${count}/${familyCounts.get(family)} divergent reorders accepted`);
  const unobserved = Object.keys(requiredFamilies).filter((family) => !observedGapFamilies.has(family));
  if (unobserved.length) throw new Error(`no known gap reproduced for ${unobserved.join(", ")}`);
}
console.log(`${cases.length} cases`);
