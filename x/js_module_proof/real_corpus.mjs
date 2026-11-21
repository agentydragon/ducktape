import { createRequire } from "node:module";
import { mkdir, mkdtemp, readFile, writeFile } from "node:fs/promises";
import { basename, join, resolve } from "node:path";
import { generateBindings, validateBindings } from "./bindings.mjs";

const { parse } = createRequire(import.meta.url)(process.env.JS_MODULE_PROOF_ACORN ?? "acorn");

// Try a baseline and two splits independently for each actual input file. This
// reports checker coverage. The validator may execute recognized inputs while
// seeking an unsafe witness, so run this only on trusted source files.
export async function assessCorpus(paths, outputDir) {
  await mkdir(outputDir, { recursive: true });
  const summary = {
    programs: 0,
    assignments: 0,
    proved: 0,
    unsafe: 0,
    unknown: 0,
    generationRefused: 0,
    validationUnknown: 0,
    unknownReasons: {},
    entries: [],
  };
  for (const path of paths) {
    summary.programs++;
    const text = await readFile(path, "utf8");
    let body;
    try {
      body = parse(text, { ecmaVersion: "latest", sourceType: "module" }).body;
    } catch (error) {
      summary.entries.push({ path, result: "parse-error", reason: String(error) });
      continue;
    }
    const count = body.filter((node) => node.type !== "ExportNamedDeclaration" || node.declaration).length;
    const indices = Array.from({ length: count }, (_, index) => index);
    const assignments = {
      baseline: { "entry.mjs": { imports: [], statements: indices } },
      split: {
        "first.mjs": { imports: [], statements: indices.filter((index) => index % 2 === 0) },
        "second.mjs": { imports: ["first.mjs"], statements: indices.filter((index) => index % 2 === 1) },
        "entry.mjs": { imports: ["first.mjs", "second.mjs"], statements: [] },
      },
      contiguous: {
        "first.mjs": { imports: [], statements: indices.slice(0, Math.ceil(count / 2)) },
        "second.mjs": { imports: ["first.mjs"], statements: indices.slice(Math.ceil(count / 2)) },
        "entry.mjs": { imports: ["first.mjs", "second.mjs"], statements: [] },
      },
    };
    for (const [assignment, modules] of Object.entries(assignments)) {
      summary.assignments++;
      const root = await mkdtemp(join(outputDir, `${basename(path).replace(/[^A-Za-z0-9_-]/g, "_")}-`));
      const source = join(root, "source.mjs"),
        spec = join(root, "assignment.json"),
        out = join(root, "out");
      await writeFile(source, text);
      await writeFile(spec, JSON.stringify({ kind: "bindings", entry: "entry.mjs", modules }, null, 2) + "\n");
      let verdict;
      let stage = "generation";
      try {
        await generateBindings(source, spec, out);
        stage = "validation";
        verdict = await validateBindings(source, spec, out);
      } catch (error) {
        if (!String(error).includes("unknown:")) throw error;
        verdict = { result: "unknown", reason: String(error) };
      }
      const category = verdict.result === "proved under assumptions" ? "proved" : verdict.result;
      if (!Object.hasOwn(summary, category)) throw new Error(`unexpected verdict: ${verdict.result}`);
      summary[category]++;
      if (category === "unknown") {
        if (stage === "generation") summary.generationRefused++;
        else summary.validationUnknown++;
        const reason = verdict.reason ?? "unspecified";
        summary.unknownReasons[reason] = (summary.unknownReasons[reason] ?? 0) + 1;
      }
      const entry = {
        path,
        assignment,
        stage,
        result: verdict.result,
        reason: verdict.reason ?? null,
        artifacts: root,
      };
      summary.entries.push(entry);
      await writeFile(join(root, "verdict.json"), JSON.stringify(verdict, null, 2) + "\n");
    }
  }
  return summary;
}

if (process.argv[1] && resolve(process.argv[1]) === resolve(new URL(import.meta.url).pathname)) {
  const [outputDir, ...paths] = process.argv.slice(2);
  if (!outputDir || !paths.length) throw new Error("usage: real_corpus.mjs OUTPUT_DIR SOURCE...");
  console.log(JSON.stringify(await assessCorpus(paths, resolve(outputDir)), null, 2));
}
