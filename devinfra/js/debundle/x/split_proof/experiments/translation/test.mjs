// Runs the translation checker on every fixture: the fixture itself must be accepted, and every variant (a small
// edit of the output tree: a find-and-replace in a file, or a new file) must give the verdict in its `variants.json`.
//
//   node test.mjs [--keep]
import { spawnSync } from "node:child_process";
import { cpSync, existsSync, mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const translate = path.join(here, "../../lean/.lake/build/bin/translate");
const run = (command, args) => spawnSync(command, args, { encoding: "utf8", maxBuffer: 1 << 28 });

let failures = 0;
for (const name of readdirSync(path.join(here, "fixtures"))) {
  const fixture = path.join(here, "fixtures", name);
  const work = mkdtempSync(path.join(tmpdir(), "translation_"));
  const bundleJson = path.join(work, "bundle.json");
  const front = run("node", [
    path.join(here, "frontend.mjs"),
    "bundle",
    path.join(fixture, "bundle.js"),
    "--out",
    bundleJson,
  ]);
  if (front.status !== 0) throw new Error(front.stderr);
  const check = (outDir, certMode, baseCert) => {
    const outputJson = path.join(work, "output.json");
    const tree = run("node", [
      path.join(here, "frontend.mjs"),
      "tree",
      outDir,
      "--entry",
      "entry.js",
      "--out",
      outputJson,
    ]);
    if (tree.status !== 0) return { code: -1, text: tree.stderr };
    const cert = certMode === "base" ? baseCert : path.join(work, "cert.json");
    if (certMode !== "base") run("node", [path.join(here, "pair.mjs"), bundleJson, outputJson, "--out", cert]);
    const result = run(translate, [bundleJson, outputJson, cert]);
    return { code: result.status, text: result.stdout + result.stderr };
  };
  // the fixture itself
  const baseOut = path.join(fixture, "out");
  const baseCert = path.join(work, "base_cert.json");
  const base = check(baseOut, "match");
  writeFileSync(baseCert, readFileSync(path.join(work, "cert.json")));
  const baseOutput = path.join(work, "base_output.json");
  writeFileSync(baseOutput, readFileSync(path.join(work, "output.json")));
  const cases = [{ name: "(unchanged)", expect: { code: 0, contains: "PLAN SOUND" }, run: () => base }];
  const variantsFile = path.join(fixture, "variants.json");
  for (const variant of existsSync(variantsFile) ? JSON.parse(readFileSync(variantsFile, "utf8")) : []) {
    cases.push({
      name: variant.name,
      expect: variant.expect,
      run: () => {
        const copy = path.join(work, "variant");
        rmSync(copy, { recursive: true, force: true });
        cpSync(baseOut, copy, { recursive: true });
        for (const edit of variant.edits) {
          const file = path.join(copy, edit.file);
          if (edit.create !== undefined) {
            writeFileSync(file, edit.create);
            continue;
          }
          const text = readFileSync(file, "utf8");
          if (!text.includes(edit.find))
            throw new Error(`${name}/${variant.name}: ${edit.file} does not contain ${JSON.stringify(edit.find)}`);
          writeFileSync(file, text.replace(edit.find, edit.replace));
        }
        return check(copy, variant.cert, baseCert);
      },
    });
  }
  for (const c of cases) {
    const got = c.run();
    const ok = got.code === c.expect.code && got.text.includes(c.expect.contains);
    if (!ok) failures++;
    const line = (
      got.text.split("\n").find((l) => /^(TRANSLATION REJECT|PLAN|OBLIGATIONS)/.test(l)) ?? got.text.split("\n")[0]
    ).slice(0, 110);
    console.log(`${ok ? "pass" : "FAIL"}  ${name}: ${c.name}  [exit ${got.code}] ${line}`);
  }
  // every mutant of the accepted output must be rejected
  const mutants = run("node", [path.join(here, "mutate.mjs"), bundleJson, baseOutput, baseCert, "--count", "24"]);
  const survivors = mutants.stdout.split("\n").filter((l) => l.startsWith("SURVIVED"));
  if (mutants.status !== 0 && survivors.length === 0) throw new Error(mutants.stderr);
  failures += survivors.length;
  console.log(
    `${survivors.length === 0 ? "pass" : "FAIL"}  ${name}: ${mutants.stdout.split("\n").filter((l) => l.startsWith("rejected")).length} mutants rejected, ${survivors.length} survived`
  );
  for (const line of survivors) console.log(`      ${line}`);
  if (!process.argv.includes("--keep")) rmSync(work, { recursive: true, force: true });
}
if (failures) throw new Error(`${failures} fixture cases gave the wrong verdict`);
