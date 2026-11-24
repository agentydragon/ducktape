// What can each generator of test programs produce? Samples every generator, parses the programs that come out, runs the
// ones that parse under `intrinsics_probe.mjs`, and tabulates, per construct, in how many programs that complete it
// occurs:
//
//   - ESTree node types and syntax forms of ES2024 (`syntax_inventory.mjs`), next to how the effect analysis treats the
//     standalone witness during module initialization;
//   - the rows of the table of standard functions (`Intrinsics.lean`): programs that call the function;
//   - calls of standard functions the table has no row for (each is a call into the world for the analysis).
//
//   node coverage_audit.mjs [--samples N] [--seed S] [--jobs J] [--only id,id] [--analyzer FILE] [--out COVERAGE.md]
//
// Only programs that complete count: premise P4 drops the rest from every oracle, so a construct that occurs only in
// programs that throw is as good as missing. The table is a measurement of the sampled seeds, not a proof that a
// generator cannot produce a construct.
import { mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { readAnalysisInventory } from "./analysis_inventory.mjs";
import { shiftCandidate } from "./shift_candidate.mjs";
import { FORMS, NODE_TYPES, featuresOf, parseModule } from "./syntax_inventory.mjs";
import { spawnAndWait } from "./spawn_and_wait.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const probe = path.join(here, "intrinsics_probe.mjs");

// ---- the generators

/** The bundles `fuzz.mjs --emit` writes for a corpus mode. */
const fuzzBundles =
  (flags) =>
  async ({ count, seed, work }) => {
    const dir = path.join(work, "emit");
    const r = await spawnAndWait(process.execPath, [
      path.join(here, "fuzz.mjs"),
      "--emit",
      dir,
      "--count",
      String(count),
      "--seed",
      String(seed),
      ...flags,
    ]);
    if (r.status !== 0) throw new Error(`fuzz.mjs --emit ${flags.join(" ")} exited ${r.status}: ${r.stderr}`);
    return readdirSync(dir)
      .sort((a, b) => Number(a.match(/\d+/)) - Number(b.match(/\d+/)))
      .map((f) => ({ source: readFileSync(path.join(dir, f), "utf8") }));
  };

/** Sample the emitter test's candidates before it appends its binding observer. */
const shiftPrograms =
  (options) =>
  ({ count, seed }) =>
    Array.from({ length: count }, (_, i) => {
      const { source, marker, skip } = shiftCandidate(seed + i, options);
      if (skip) return { source, skipped: skip === "parseRejected" ? "does not parse" : "the test skips it" };
      return { source, prelude: marker ? `globalThis.${marker} = (kind, value) => value;\n` : "" };
    });

export const GENERATORS = [
  { id: "templates", title: "`fuzz.mjs`: unit templates", produce: fuzzBundles([]) },
  { id: "callback", title: "`fuzz.mjs --callback-corpus`", produce: fuzzBundles(["--callback-corpus"]), maxCount: 16 },
  { id: "ast", title: "`fuzz.mjs --ast-corpus`: `ast_generator.mjs`", produce: fuzzBundles(["--ast-corpus"]) },
  {
    id: "full",
    title: "`fuzz.mjs --full-corpus`: `gen/`, the generator of the whole language",
    produce: fuzzBundles(["--full-corpus"]),
  },
  {
    id: "shift",
    title: "Shift module grammar (`shift_fuzz_test`)",
    produce: shiftPrograms({ script: false, modern: false }),
  },
  {
    id: "shift-script",
    title: "Shift script grammar (`shift_script_fuzz_test`)",
    produce: shiftPrograms({ script: true, modern: false }),
  },
  {
    id: "shift-modern",
    title: "Shift script grammar with modern expressions (`shift_modern_fuzz_test`)",
    produce: shiftPrograms({ script: true, modern: true }),
  },
];

// ---- running programs

const errorClass = (stderr) =>
  (stderr.match(/\b(ReferenceError|TypeError|SyntaxError|RangeError|URIError|EvalError|Error)\b/) ?? ["unknown"])[1];

/** Run one program under the probe: `{ status: "ok" | "throws" | "timeout", error?, counts }`. */
async function runProgram({ source, prelude = "" }, dir, name, keysFile) {
  const file = path.join(dir, `${name}.mjs`);
  const out = path.join(dir, `${name}.probe.json`);
  writeFileSync(file, prelude + source);
  const r = await spawnAndWait(
    process.execPath,
    ["--max-old-space-size=128", "--import", pathToFileURL(probe).href, file],
    {
      env: { ...process.env, PROBE_KEYS: keysFile, PROBE_OUT: out, PROBE_PROGRAM: pathToFileURL(file).href },
      timeoutMs: 4000,
    }
  );
  let counts = {};
  try {
    counts = JSON.parse(readFileSync(out, "utf8")).counts;
  } catch {
    // killed before the exit handler wrote it
  }
  if (r.timedOut) return { status: "timeout", counts };
  return r.status === 0 ? { status: "ok", counts } : { status: "throws", error: errorClass(r.stderr), counts };
}

/** Sample one generator: how many programs, how many parse, how many complete, and the constructs of the complete ones. */
export async function sampleGenerator(gen, { count, seed, work, jobs, keysFile }) {
  const dir = path.join(work, gen.id);
  mkdirSync(dir, { recursive: true });
  const programs = await gen.produce({ count: Math.min(count, gen.maxCount ?? count), seed, work: dir });
  const sample = {
    id: gen.id,
    generated: programs.length,
    skipped: {},
    status: { ok: 0, throws: 0, timeout: 0 },
    errors: {},
    types: new Map(),
    forms: new Map(),
    rows: new Map(),
    typesInThrowing: new Set(),
    formsInThrowing: new Set(),
    statements: 0,
  };
  const bump = (map, key) => map.set(key, (map.get(key) ?? 0) + 1);
  let next = 0;
  await Promise.all(
    Array.from({ length: jobs }, async () => {
      while (next < programs.length) {
        const i = next++;
        const program = programs[i];
        if (program.skipped) {
          sample.skipped[program.skipped] = (sample.skipped[program.skipped] ?? 0) + 1;
          continue;
        }
        let features;
        let ast;
        try {
          ast = parseModule(program.source);
          features = featuresOf(ast);
        } catch {
          sample.skipped["does not parse"] = (sample.skipped["does not parse"] ?? 0) + 1;
          continue;
        }
        const result = await runProgram(program, dir, `p${i}`, keysFile);
        sample.status[result.status]++;
        if (result.error) sample.errors[result.error] = (sample.errors[result.error] ?? 0) + 1;
        if (result.status !== "ok") {
          for (const t of features.types.keys()) sample.typesInThrowing.add(t);
          for (const f of features.forms.keys()) sample.formsInThrowing.add(f);
          continue;
        }
        sample.statements += ast.body.length;
        for (const t of features.types.keys()) bump(sample.types, t);
        for (const f of features.forms.keys()) bump(sample.forms, f);
        for (const [key, calls] of Object.entries(result.counts)) if (calls > 0) bump(sample.rows, key);
      }
    })
  );
  return sample;
}

// ---- the report

function witnessOutcome({ effectUnits, unknown, outOfBudget }) {
  const details = [effectUnits.length ? `world effect in unit ${effectUnits.join(", ")}` : "no world effect"];
  if (unknown.length) details.push(`unknown: ${unknown.join(", ")}`);
  if (outOfBudget) details.push(`out of budget: ${outOfBudget}`);
  return details.join("; ");
}

const cell = (n) => (n === 0 ? "·" : String(n));

function table(header, rows, align) {
  const line = (cells) => `| ${cells.join(" | ")} |`;
  return [line(header), line(header.map((_, i) => (align[i] === "r" ? "---:" : "---"))), ...rows.map(line)].join("\n");
}

const ROW_FAMILIES = [
  ["Object, Reflect", /^(Object|Reflect)\./],
  ["Function", /^Function\./],
  ["Array", /^Array/],
  ["Map, Set", /^(Map|Set|WeakMap|WeakSet)/],
  [
    "String, Number, Boolean, BigInt, Symbol, primitives",
    /^(prim\.|String|Number|Boolean|BigInt|Symbol|parse|is|encode|decode)/,
  ],
  ["Math, JSON, Intl", /^(Math|JSON|Intl)/],
  [
    "Error, RegExp, Promise, web",
    /^(Error|TypeError|RangeError|SyntaxError|ReferenceError|RegExp|Promise|TextEncoder|TextDecoder|URLSearchParams|DOMException)/,
  ],
  ["environment", /^env:/],
];

/** Keys the probe cannot count in Node: a browser's environment, and members that are not functions. */
const notProbeable = (key) =>
  /^env:(window|document|localStorage|sessionStorage|requestAnimationFrame)/.test(key) ||
  /^(Math\.(PI|E)|Number\.(MAX_SAFE_INTEGER|MIN_SAFE_INTEGER|EPSILON)|prim\.description)$/.test(key);

/** A probed key the analysis has a row for: a row of the table, or the method of a primitive's wrapper, which `prim.` covers. */
const isModeled = (key, tableKeys) =>
  tableKeys.has(key) || /^(String|Number|Boolean|BigInt|Symbol)\.prototype\./.test(key);

/** The Markdown report. */
export function renderReport({ inventory, samples, count, seed }) {
  const out = [];
  const cols = samples.map((s) => `\`${s.id}\``);
  const align = ["l", "l", ...samples.map(() => "r")];
  out.push("# Coverage of the program generators", "");
  out.push(
    `Generated by \`node coverage_audit.mjs --samples ${count} --seed ${seed}\` (then \`prettier --write\`). Each cell is the number of programs that complete and contain the construct; \`·\` is none. Only programs that complete count, because premise P4 drops the others from every oracle. It is a measurement of the sampled seeds: a zero says the generator did not produce the construct in those programs, and a count says it can.`,
    ""
  );
  out.push("## Generators", "");
  out.push(
    table(
      [
        "id",
        "generator",
        "generated",
        "completed",
        "throw",
        "time out",
        "do not parse or are skipped",
        "top-level statements per completed program",
      ],
      samples.map((s) => [
        `\`${s.id}\``,
        GENERATORS.find((g) => g.id === s.id).title,
        s.generated,
        s.status.ok,
        s.status.throws,
        s.status.timeout,
        Object.values(s.skipped).reduce((a, b) => a + b, 0),
        s.status.ok ? (s.statements / s.status.ok).toFixed(1) : "-",
      ]),
      ["l", "l", "r", "r", "r", "r", "r", "r"]
    ),
    ""
  );
  // one line per generator: how much of each inventory it reaches
  const probeable = inventory.tableKeys.filter((k) => !notProbeable(k));
  const tableKeySet = new Set(inventory.tableKeys);
  const reached = (s) => ({
    types: NODE_TYPES.filter(([t]) => s.types.has(t)).length,
    forms: FORMS.filter((f) => s.forms.has(f.name)).length,
    rows: probeable.filter((k) => s.rows.has(k)).length,
    unmodeled: [...s.rows.keys()].filter((k) => !isModeled(k, tableKeySet)).length,
  });
  const union = { types: new Set(), forms: new Set(), rows: new Set(), unmodeled: new Set() };
  for (const s of samples) {
    for (const [t] of NODE_TYPES) if (s.types.has(t)) union.types.add(t);
    for (const f of FORMS) if (s.forms.has(f.name)) union.forms.add(f.name);
    for (const k of probeable) if (s.rows.has(k)) union.rows.add(k);
    for (const k of s.rows.keys()) if (!isModeled(k, tableKeySet)) union.unmodeled.add(k);
  }
  out.push("## Summary", "");
  out.push(
    table(
      [
        "generator",
        `node types (of ${NODE_TYPES.length})`,
        `syntax forms (of ${FORMS.length})`,
        `rows called (of ${probeable.length})`,
        "other standard functions called",
      ],
      [
        ...samples.map((s) => {
          const r = reached(s);
          return [`\`${s.id}\``, r.types, r.forms, r.rows, r.unmodeled];
        }),
        ["**all together**", union.types.size, union.forms.size, union.rows.size, union.unmodeled.size],
      ],
      ["l", "r", "r", "r", "r"]
    ),
    ""
  );
  const throwing = samples.filter((s) => Object.keys(s.errors).length > 0);
  if (throwing.length > 0) {
    out.push("Why the programs that throw do:", "");
    for (const s of throwing)
      out.push(
        `- \`${s.id}\`: ${Object.entries(s.errors)
          .sort((a, b) => b[1] - a[1])
          .map(([e, n]) => `${e} ${n}`)
          .join(", ")}`
      );
    out.push("");
  }

  // node types
  out.push("## ESTree node types", "");
  out.push("Each count includes occurrences anywhere in a completed generated program, including deferred bodies.", "");
  const typeRows = NODE_TYPES.map(([type, since]) => [
    `\`${type}\``,
    since,
    ...samples.map((s) => cell(s.types.get(type) ?? 0)),
  ]);
  out.push(table(["node type", "since", ...cols], typeRows, align), "");

  out.push("## Syntax forms", "");
  out.push(
    "Lean analyzes each form’s witness snippet as a separate module under the default premises. The observation covers the whole snippet during initialization. A form inside a deferred body may have no initialization effect.",
    ""
  );
  const formRows = FORMS.map((f) => [
    `\`${f.name}\``,
    `${f.since}; ${witnessOutcome(inventory.witnesses[f.name])}`,
    ...samples.map((s) => cell(s.forms.get(f.name) ?? 0)),
  ]);
  out.push(table(["form", "since; witness observation", ...cols], formRows, align), "");

  // rows
  const tableKeys = new Set(inventory.tableKeys);
  out.push("## Standard functions the analysis has a row for", "");
  out.push(
    `The keys of \`table\` in \`Intrinsics.lean\` (${tableKeys.size}), by the function the probe replaced: the number of programs that call it from their own code. A key that is not a function (\`Math.PI\`) and the environment of a browser (\`env:window.*\`, \`env:localStorage.*\`) cannot be probed in Node and are marked \`n/a\`.`,
    ""
  );
  const placed = new Set();
  for (const [family, pattern] of ROW_FAMILIES) {
    const keys = inventory.tableKeys.filter((k) => !placed.has(k) && pattern.test(k));
    keys.forEach((k) => placed.add(k));
    if (keys.length === 0) continue;
    out.push(`### ${family}`, "");
    out.push(
      table(
        ["row", ...cols],
        keys.map((k) => [`\`${k}\``, ...samples.map((s) => (notProbeable(k) ? "n/a" : cell(s.rows.get(k) ?? 0)))]),
        ["l", ...samples.map(() => "r")]
      ),
      ""
    );
  }
  const leftover = inventory.tableKeys.filter((k) => !placed.has(k));
  if (leftover.length > 0) {
    out.push(
      "### other",
      "",
      table(
        ["row", ...cols],
        leftover.map((k) => [`\`${k}\``, ...samples.map((s) => cell(s.rows.get(k) ?? 0))]),
        ["l", ...samples.map(() => "r")]
      ),
      ""
    );
  }

  // unmodeled calls
  const unmodeled = new Set();
  for (const s of samples) for (const key of s.rows.keys()) if (!isModeled(key, tableKeys)) unmodeled.add(key);
  const members = new Set(inventory.memberPaths);
  out.push("## Standard functions called without a row", "");
  out.push(
    `A call of a standard function that has no row is a call into the world for the analysis: the unit touches the world and everything the call received escapes. The probe wraps the ${members.size} members the analysis lists for the standard objects (\`IntrinsicMembers.lean\`); these are the ones that sampled programs call.`,
    ""
  );
  out.push(
    table(
      ["member", ...cols],
      [...unmodeled].sort().map((k) => [`\`${k}\``, ...samples.map((s) => cell(s.rows.get(k) ?? 0))]),
      ["l", ...samples.map(() => "r")]
    ),
    ""
  );

  // gaps
  out.push("## Gaps", "");
  const none = (items, get) => items.filter((x) => samples.every((s) => get(s, x) === 0));
  const missingTypes = none(
    NODE_TYPES.map(([t]) => t),
    (s, t) => s.types.get(t) ?? 0
  );
  out.push(
    `**Node types no generator produced in a program that completes** (${missingTypes.length} of ${NODE_TYPES.length}): ${missingTypes.map((t) => `\`${t}\``).join(", ") || "none"}.`,
    ""
  );
  const missingForms = none(
    FORMS.map((f) => f.name),
    (s, f) => s.forms.get(f) ?? 0
  );
  out.push(
    `**Syntax forms no generator produced** (${missingForms.length} of ${FORMS.length}): ${missingForms.map((f) => `\`${f}\``).join(", ") || "none"}.`,
    ""
  );
  const missingRows = none(
    inventory.tableKeys.filter((k) => !notProbeable(k)),
    (s, k) => s.rows.get(k) ?? 0
  );
  out.push(
    `**Rows of the table no generator called** (${missingRows.length} of ${inventory.tableKeys.filter((k) => !notProbeable(k)).length}): ${missingRows.map((k) => `\`${k}\``).join(", ") || "none"}.`,
    ""
  );
  for (const s of samples) {
    const onlyThrowing = [...s.formsInThrowing].filter((f) => !s.forms.has(f));
    if (onlyThrowing.length > 0)
      out.push(
        `- \`${s.id}\` produces ${onlyThrowing.length} forms only in programs that throw: ${onlyThrowing
          .sort()
          .map((f) => `\`${f}\``)
          .join(", ")}.`
      );
  }
  out.push("");
  return out.join("\n");
}

async function main() {
  const args = process.argv.slice(2);
  const option = (name, fallback) => (args.includes(`--${name}`) ? args[args.indexOf(`--${name}`) + 1] : fallback);
  const count = Number(option("samples", 100));
  const seed = Number(option("seed", 1));
  const jobs = Number(option("jobs", 2));
  const only = option("only", undefined)?.split(",");
  const leanDir = option("lean", undefined);
  const executable = option("analyzer", leanDir && path.resolve(leanDir, "../.lake/build/bin/analysis_inventory"));
  const out = option("out", undefined);
  const inventory = readAnalysisInventory(executable);
  const work = mkdtempSync(path.join(process.env.TMPDIR ?? os.tmpdir(), "coverage_audit_"));
  try {
    const keysFile = path.join(work, "keys.json");
    writeFileSync(keysFile, JSON.stringify([...new Set([...inventory.tableKeys, ...inventory.memberPaths])]));
    const samples = [];
    for (const gen of GENERATORS.filter((g) => !only || only.includes(g.id))) {
      console.error(`sampling ${gen.id}`);
      samples.push(await sampleGenerator(gen, { count, seed, work, jobs, keysFile }));
    }
    const report = renderReport({ inventory, samples, count, seed });
    if (out) writeFileSync(out, report);
    else console.log(report);
  } finally {
    rmSync(work, { recursive: true, force: true });
  }
}

if (process.argv[1] === fileURLToPath(import.meta.url)) await main();
