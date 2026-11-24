// Fuzzer for the translation checker: does `translate` accept a tree that behaves differently from its bundle?
//
//   node fuzz.mjs [--count N] [--seed S] [--trees T] [--jobs J] [--leaks] [--callback-corpus] [--ast-corpus] [--out DIR] [--keep] [--require-accepted]
//   node fuzz.mjs [--count N] [--seed S] [--leaks] [--callback-corpus] [--ast-corpus] [--full-corpus] --emit DIR
//
// `--emit DIR` writes generated source programs and stops before checking or executing module trees.
import { spawn } from "node:child_process";
import { cpSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { frontBundle, frontTree } from "./frontend.mjs";
import { seededRandom } from "../seeded_random.mjs";
import { astStatistics, generateCorpusBundle, renderBundle, split } from "./fuzz_support.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const translate = process.env.TRANSLATE ?? path.join(here, "../lean/.lake/build/bin/translate");
// The premises the checker runs under besides the always-on ones: the world defines no accessors or conversion hooks on
// program objects (P3), which the generated bundles satisfy.
const premises = { noWorldHooks: true };

// `lean_binary` runs the checker in Lean's interpreter, which takes seconds a call on a busy worker: a hang is a minute.
function run(command, args) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { stdio: ["ignore", "pipe", "pipe"] });
    let stdout = "";
    let stderr = "";
    const timer = setTimeout(() => child.kill("SIGKILL"), 60000);
    child.stdout.on("data", (d) => (stdout += d));
    child.stderr.on("data", (d) => (stderr += d));
    child.on("error", (error) => {
      clearTimeout(timer);
      reject(error);
    });
    child.on("close", (status, signal) => {
      clearTimeout(timer);
      if (signal) {
        reject(new Error(`${command} ${args.join(" ")} terminated by ${signal}`));
        return;
      }
      resolve({ status, stdout, stderr });
    });
  });
}

const behavior = ({ status, stdout, stderr }) => ({
  status,
  stdout,
  error:
    status === 0 ? "" : (stderr.match(/\b(ReferenceError|TypeError|SyntaxError|RangeError|Error)\b/) ?? ["unknown"])[1],
});
const sameBehavior = (a, b) => a.status === b.status && a.stdout === b.stdout && a.error === b.error;

function differsOnlyAtNameProbes(bundle, plan, reference, observed) {
  if (!plan.rename || plan.capture || reference.status !== 0 || observed.status !== 0) return false;
  const probes = new Set(bundle.units.flatMap((unit, index) => (unit.tmpl === "fnName" ? [String(index)] : [])));
  const mask = (stdout) =>
    stdout.replace(/^name#(\d+) \d+$/gm, (line, index) => (probes.has(index) ? `name#${index}` : line));
  return probes.size > 0 && mask(reference.stdout) === mask(observed.stdout);
}

const checkerVerdicts = new Map([
  [0, { name: "accept", output: /^PLAN SOUND$/m }],
  [1, { name: "reject", output: /^(TRANSLATION REJECT|PLAN NOT SOUND):/m }],
  [4, { name: "obligations", output: /^OBLIGATIONS:/m }],
]);

async function main() {
  const args = process.argv.slice(2);
  const option = (name, fallback) => (args.includes(`--${name}`) ? args[args.indexOf(`--${name}`) + 1] : fallback);
  const count = Number(option("count", 100));
  const seed = Number(option("seed", 1));
  const trees = Number(option("trees", 6));
  const jobs = Number(option("jobs", Math.max(1, os.cpus().length)));
  const leaks = args.includes("--leaks");
  const callbackCorpus = args.includes("--callback-corpus");
  const astCorpus = args.includes("--ast-corpus");
  // `--full-corpus`: programs of `gen/` (every family, or `--families a,b`), which reach any part of the language
  const fullCorpus = args.includes("--full-corpus");
  const families = option("families", undefined)?.split(",");
  if ([callbackCorpus, astCorpus, fullCorpus].filter(Boolean).length > 1)
    throw new Error("choose one structured corpus");
  const astTypes = {};
  const astFeatures = {};
  const planTypes = {};
  let astDepth = 0;
  const outDir = option("out", process.env.TEST_UNDECLARED_OUTPUTS_DIR);
  const emitDir = option("emit", undefined);
  if (emitDir) mkdirSync(emitDir, { recursive: true });
  const work = mkdtempSync(path.join(process.env.TEST_TMPDIR ?? os.tmpdir(), "translation_fuzz_"));
  const tally = {};
  const wrongAccepts = [];
  const assumedAccepts = [];
  const wrongRejects = [];
  const checkerCrashes = []; // trees on which the checker ended with a signal (it ran out of stack, say)
  let invalidBundles = 0;
  let refused = 0;
  const bump = (key) => (tally[key] = (tally[key] ?? 0) + 1);

  const oneBundle = async (round) => {
    const bundle = generateCorpusBundle({ seed, round, astCorpus, callbackCorpus, fullCorpus, leaks, families });
    if (astCorpus) {
      const stats = astStatistics(bundle);
      astDepth = Math.max(astDepth, stats.maxDepth);
      for (const [type, n] of Object.entries(stats.types)) astTypes[type] = (astTypes[type] ?? 0) + n;
      for (const [feature, n] of Object.entries(stats.features)) astFeatures[feature] = (astFeatures[feature] ?? 0) + n;
    }
    if (emitDir) {
      writeFileSync(path.join(emitDir, `bundle_${round}.mjs`), astCorpus ? bundle.source : renderBundle(bundle));
      return;
    }
    const dir = path.join(work, `b${round}`);
    mkdirSync(dir, { recursive: true });
    const bundleFile = path.join(dir, "bundle.mjs");
    writeFileSync(bundleFile, astCorpus ? bundle.source : renderBundle(bundle));
    const reference = behavior(await run(process.execPath, [bundleFile]));
    if (reference.status !== 0) {
      invalidBundles++;
      if (invalidBundles <= 3) console.log(`generator bug: bundle ${round} fails in Node (${reference.error})`);
      return;
    }
    const bundleJson = path.join(dir, "bundle.json");
    const premisesJson = path.join(dir, "premises.json");
    writeFileSync(premisesJson, JSON.stringify(premises));
    try {
      writeFileSync(bundleJson, JSON.stringify(frontBundle(bundleFile)));
    } catch {
      refused++;
      return;
    }
    for (let t = 0; t < trees; t++) {
      const tr = seededRandom(seed * 7919 + round * 131 + t);
      const place = callbackCorpus
        ? bundle.units.map((_, i) => (t % 2 === 1 ? Number(bundle.side.includes(i)) : round % 2))
        : astCorpus && t === 0
          ? bundle.units.map(() => 0)
          : astCorpus && t === 1 && bundle.guidedPlace
            ? bundle.units.map(() => 0)
            : astCorpus && t === 2 && bundle.writeGuided
              ? bundle.units.map(() => 0)
              : undefined;
      const plan = split(bundle, tr, {
        renameLocals: !callbackCorpus && tr.chance(0.5),
        place,
        moveBefore: astCorpus && t === 1 ? bundle.guidedMove : astCorpus && t === 2 ? bundle.writeGuided : undefined,
        controlName:
          astCorpus && t === 0
            ? "astControl"
            : astCorpus && t === 1 && bundle.guidedPlace
              ? "astGuided"
              : astCorpus && t === 2 && bundle.writeGuided
                ? "astWriteGuided"
                : undefined,
      });
      if (astCorpus) planTypes[plan.strategy] = (planTypes[plan.strategy] ?? 0) + 1;
      const treeDir = path.join(dir, `t${t}`, "out");
      mkdirSync(treeDir, { recursive: true });
      writeFileSync(path.join(treeDir, "package.json"), '{"type":"module"}');
      for (const [name, text] of Object.entries(plan.files)) writeFileSync(path.join(treeDir, name), text);
      const outputJson = path.join(dir, `t${t}`, "output.json");
      const certJson = path.join(dir, `t${t}`, "cert.json");
      try {
        writeFileSync(outputJson, JSON.stringify(frontTree(treeDir, "entry.js")));
      } catch {
        refused++;
        continue;
      }
      writeFileSync(certJson, JSON.stringify({ pairs: plan.mapping }));
      let checked;
      let ran;
      try {
        [checked, ran] = await Promise.all([
          run(translate, [bundleJson, outputJson, certJson, "--premises", premisesJson]),
          run(process.execPath, [path.join(treeDir, "entry.js")]),
        ]);
      } catch (error) {
        // a checker that does not finish on a valid bundle is a finding about it, reported after the run, not a stop
        if (!error.message.startsWith(`${translate} `)) throw error;
        checkerCrashes.push({ round, tree: t, message: error.message.replace(/^.* terminated by /, "terminated by ") });
        continue;
      }
      const tree = behavior(ran);
      const same = sameBehavior(reference, tree);
      const outcome = checkerVerdicts.get(checked.status);
      if (!outcome?.output.test(checked.stdout))
        throw new Error(
          `bundle ${round} tree ${t}: unexpected translate exit ${checked.status}\n${checked.stdout}${checked.stderr}`
        );
      const verdict = outcome.name;
      if (astCorpus && plan.strategy === "astGuided" && !same) {
        bump("ast/guidedDifferent");
        if (bundle.guidedCallbackShape !== "direct") {
          bump("ast/indirectGuidedDifferent");
          if (bundle.guidedInitializer === "ArrowFunctionExpression") bump("ast/indirectArrowGuidedDifferent");
          if (bundle.guidedInitializer === "Literal") bump("ast/indirectLiteralGuidedDifferent");
          if (verdict === "obligations") bump("ast/indirectGuidedObligations");
        }
      }
      if (astCorpus && plan.strategy === "astWriteGuided" && !same) {
        bump("ast/writeGuidedDifferent");
        if (verdict === "obligations") bump("ast/writeGuidedObligations");
      }
      if (
        astCorpus &&
        verdict === "accept" &&
        same &&
        (plan.modules > 1 || plan.mapping.some(([, , original], index) => original !== index))
      )
        bump("ast/nontrivialAccepted");
      if (astCorpus && t === 0 && (!same || verdict !== "accept"))
        throw new Error(`AST safe control ${round}: ${verdict}, same=${same}`);
      if (callbackCorpus && t % 2 === 0 && (!same || verdict !== "accept"))
        throw new Error(`${bundle.caseName}: safe control failed: ${verdict}, same=${same}`);
      if (callbackCorpus && t % 2 === 1 && same)
        throw new Error(`${bundle.caseName}: intended unsafe move did not change Node behavior`);
      if (callbackCorpus && t % 2 === 1 && verdict === "reject")
        throw new Error(`${bundle.caseName}: unsafe move was rejected for a structural reason: ${checked.stdout}`);
      if (callbackCorpus) bump(`${bundle.caseName}/${t % 2 ? "unsafe" : "safe"}`);
      const intent = plan.capture ? "capture" : "relocation";
      bump(`${verdict}/${same ? "same" : "differs"}`);
      const record = {
        seed,
        mode: astCorpus ? "ast" : callbackCorpus ? "callback" : fullCorpus ? "full" : "templates",
        round,
        tree: t,
        verdict,
        same,
        plan: { strategy: plan.strategy, modules: plan.modules, rename: plan.rename, capture: plan.capture },
        writeGuided: plan.strategy === "astWriteGuided" ? bundle.writeGuided : null,
        guidedCallbackShape: plan.strategy === "astGuided" ? bundle.guidedCallbackShape : null,
        guidedInitializer: plan.strategy === "astGuided" ? bundle.guidedInitializer : null,
        guidedMove: plan.strategy === "astGuided" ? bundle.guidedMove : null,
        templates: [...new Set(bundle.units.map((u) => u.tmpl))].sort(),
        reference,
        observed: tree,
        checker: checked.stdout
          .split("\n")
          .filter((l) => /^(TRANSLATION REJECT|PLAN|OBLIGATIONS|\s+FAIL)/.test(l))
          .join(" | "),
      };
      if (verdict === "accept" && !same && differsOnlyAtNameProbes(bundle, plan, reference, tree))
        assumedAccepts.push(record);
      else if (verdict === "accept" && !same) {
        wrongAccepts.push(record);
        if (outDir) {
          const keep = path.join(outDir, `wrong_accept_${round}_${t}`);
          mkdirSync(keep, { recursive: true });
          cpSync(treeDir, path.join(keep, "out"), { recursive: true });
          cpSync(bundleFile, path.join(keep, "bundle.mjs"));
          cpSync(bundleJson, path.join(keep, "bundle.json"));
          cpSync(outputJson, path.join(keep, "output.json"));
          cpSync(certJson, path.join(keep, "cert.json"));
          if (astCorpus) writeFileSync(path.join(keep, "ast.json"), JSON.stringify(bundle.ast, null, 2));
          writeFileSync(path.join(keep, "record.json"), JSON.stringify(record, null, 2));
        }
      }
      if (verdict === "reject" && same && intent === "relocation") wrongRejects.push(record);
    }
  };

  let next = 0;
  await Promise.all(
    Array.from({ length: jobs }, async () => {
      while (next < count) await oneBundle(next++);
    })
  );
  if (emitDir) {
    rmSync(work, { recursive: true, force: true });
    console.log(`${count} bundles written to ${emitDir}`);
    return;
  }
  if (args.includes("--keep") || (!outDir && (wrongAccepts.length > 0 || (astCorpus && invalidBundles > 0))))
    console.log(`kept ${work}`);
  else rmSync(work, { recursive: true, force: true });

  if (astCorpus && invalidBundles) throw new Error(`AST generator produced ${invalidBundles} invalid bundles`);
  if (fullCorpus && invalidBundles)
    throw new Error(`the generator of gen/ produced ${invalidBundles} bundles that fail in Node`);
  if (astCorpus)
    console.log(
      "AST corpus:",
      JSON.stringify({
        seed,
        count,
        maxDepth: astDepth,
        types: astTypes,
        features: astFeatures,
        plans: planTypes,
        guidedDifferentPlans: tally["ast/guidedDifferent"] ?? 0,
        indirectGuidedDifferentPlans: tally["ast/indirectGuidedDifferent"] ?? 0,
        indirectArrowGuidedDifferentPlans: tally["ast/indirectArrowGuidedDifferent"] ?? 0,
        indirectLiteralGuidedDifferentPlans: tally["ast/indirectLiteralGuidedDifferent"] ?? 0,
        indirectGuidedObligations: tally["ast/indirectGuidedObligations"] ?? 0,
        writeGuidedDifferentPlans: tally["ast/writeGuidedDifferent"] ?? 0,
        writeGuidedObligations: tally["ast/writeGuidedObligations"] ?? 0,
        nontrivialAcceptedPlans: tally["ast/nontrivialAccepted"] ?? 0,
      })
    );
  if (callbackCorpus && (count < 1 || trees < 2))
    throw new Error("--callback-corpus needs at least one bundle and two trees");
  if (callbackCorpus && (invalidBundles || refused))
    throw new Error(`callback corpus incomplete: ${invalidBundles} invalid bundles; ${refused} refused trees`);
  if (callbackCorpus)
    console.log(
      "callback corpus:",
      JSON.stringify(
        Object.fromEntries(
          Object.entries(tally)
            .filter(([k]) => k.includes("/global/") || k.includes("/slot/"))
            .sort(([a], [b]) => a.localeCompare(b))
        )
      )
    );
  const cell = (v, s) => tally[`${v}/${s}`] ?? 0;
  if (astCorpus && args.includes("--require-ast-coverage")) {
    const required = [
      "FunctionDeclaration",
      "ArrowFunctionExpression",
      "IfStatement",
      "ConditionalExpression",
      "AssignmentExpression",
      "ObjectExpression",
      "ArrayExpression",
      "CallExpression",
    ];
    const missing = required.filter((name) => !astTypes[name]);
    if (
      missing.length ||
      astDepth < 6 ||
      !astFeatures.returnedClosures ||
      !astFeatures.callbackStores ||
      !astFeatures.callbackCalls ||
      !astFeatures.aliases ||
      !astFeatures.forwardReferences ||
      !astFeatures.earlyCallbacks ||
      cell("obligations", "differs") + cell("reject", "differs") === 0
    )
      throw new Error(
        `AST coverage incomplete: missing=${missing.join(",")} depth=${astDepth} features=${JSON.stringify(astFeatures)}`
      );
  }
  if (astCorpus && args.includes("--require-indirect-guided") && !tally["ast/indirectGuidedObligations"])
    throw new Error("AST corpus did not reject a divergent indirect callback through OrderOk");
  if (astCorpus && args.includes("--require-updates") && !astTypes.UpdateExpression)
    throw new Error("AST corpus did not exercise mutable updates");
  console.log(
    `${count} bundles x ${trees} trees, seed ${seed}${leaks ? ", with leak templates" : ""}; ${invalidBundles} invalid bundles skipped, ${refused} trees the front end refused`
  );
  console.log("verdict       node same   node differs");
  for (const v of ["accept", "obligations", "reject"])
    console.log(`${v.padEnd(12)}  ${String(cell(v, "same")).padStart(9)}   ${String(cell(v, "differs")).padStart(12)}`);
  const byTemplate = {};
  for (const w of wrongAccepts) for (const t of w.templates) byTemplate[t] = (byTemplate[t] ?? 0) + 1;
  console.log(`WRONG ACCEPTS: ${wrongAccepts.length}`);
  for (const w of wrongAccepts.slice(0, 5)) {
    const a = w.reference.stdout.split("\n");
    const b = w.observed.stdout.split("\n");
    const at = a.findIndex((line, i) => line !== b[i]);
    console.log(
      `  bundle ${w.round} tree ${w.tree} (${w.plan.strategy}, ${w.plan.modules} modules): first difference at line ${at}: ${JSON.stringify(a.slice(Math.max(0, at - 1), at + 2))} against ${JSON.stringify(b.slice(Math.max(0, at - 1), at + 2))} ${w.observed.error}`
    );
  }
  if (wrongAccepts.length > 0)
    console.log(
      `  templates present in wrong accepts: ${Object.entries(byTemplate)
        .sort((a, b) => b[1] - a[1])
        .map(([t, k]) => `${t} ${k}`)
        .join(", ")}`
    );
  if (leaks) console.log(`accepted with only fn.name probe differences: ${assumedAccepts.length}`);
  console.log(`wrong rejects of plain relocations (checker stricter than Node needed): ${wrongRejects.length}`);
  const reasons = {};
  for (const w of wrongRejects) reasons[w.checker.slice(0, 90)] = (reasons[w.checker.slice(0, 90)] ?? 0) + 1;
  for (const [reason, k] of Object.entries(reasons)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 8))
    console.log(`  ${k}x ${reason}`);
  console.log(`CHECKER CRASHES: ${checkerCrashes.length}`);
  for (const c of checkerCrashes.slice(0, 5)) console.log(`  bundle ${c.round} tree ${c.tree}: ${c.message}`);
  if (wrongAccepts.length > 0) throw new Error(`${wrongAccepts.length} wrong accepts`);
  if (checkerCrashes.length > 0) throw new Error(`${checkerCrashes.length} crashes of the checker`);
  if (args.includes("--require-name-leak") && assumedAccepts.length === 0)
    throw new Error("no accepted rename changed a function-name probe");
  if (args.includes("--require-accepted") && cell("accept", "same") === 0)
    throw new Error("no accepted tree matched its bundle");
}

main().catch((error) => {
  console.error(error.message);
  process.exit(1);
});
