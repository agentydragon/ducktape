// Runs the translation checker on every fixture: the fixture itself must be accepted (unless it has an `expect.json`,
// which names the verdict for a bundle that uses something the checker refuses), and every variant (a small
// edit of the output tree: a find-and-replace in a file, or a new file) must give the verdict in its `variants.json`.
//
//   node test.mjs [--keep]
import { spawnSync } from "node:child_process";
import { cpSync, existsSync, mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const translate = process.env.TRANSLATE ?? path.join(here, "../lean/.lake/build/bin/translate");
const run = (command, args) => spawnSync(command, args, { encoding: "utf8", maxBuffer: 1 << 28 });

let failures = 0;
for (const name of readdirSync(path.join(here, "fixtures"))) {
  const fixture = path.join(here, "fixtures", name);
  const work = mkdtempSync(path.join(process.env.TEST_TMPDIR ?? tmpdir(), "translation_"));
  const bundleJson = path.join(work, "bundle.json");
  const expectFile = path.join(fixture, "expect.json");
  const baseExpect = existsSync(expectFile)
    ? JSON.parse(readFileSync(expectFile, "utf8"))
    : { code: 0, contains: "PLAN SOUND" };
  const front = run(process.execPath, [
    path.join(here, "frontend.mjs"),
    "bundle",
    path.join(fixture, "bundle.js"),
    "--out",
    bundleJson,
  ]);
  if (baseExpect.frontend_error) {
    if (front.status === 0 || !front.stderr.includes(baseExpect.frontend_error))
      throw new Error(`${name}: expected frontend error ${baseExpect.frontend_error}, got ${front.stderr}`);
    const original = run(process.execPath, [path.join(fixture, "bundle.js")]);
    if (original.status === 0 || !original.stderr.includes(baseExpect.node_error))
      throw new Error(`${name}: expected Node ${baseExpect.node_error}, got ${original.stderr}`);
    console.log(`pass  ${name}: frontend and Node reject invalid input`);
    if (!process.argv.includes("--keep")) rmSync(work, { recursive: true, force: true });
    continue;
  }
  if (front.status !== 0) throw new Error(front.stderr);
  const check = (outDir, certMode, baseCert) => {
    const outputJson = path.join(work, "output.json");
    const tree = run(process.execPath, [
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
    if (certMode !== "base") {
      const pairing = run(process.execPath, [path.join(here, "pair.mjs"), bundleJson, outputJson, "--out", cert]);
      if (pairing.status !== 0) throw new Error(pairing.stderr);
    }
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
  if (baseExpect.node_error) {
    const original = run(process.execPath, [path.join(fixture, "bundle.js")]);
    if (original.status !== 0) throw new Error(`${name}: original bundle failed: ${original.stderr}`);
    const emitted = run(process.execPath, [path.join(baseOut, "entry.js")]);
    if (emitted.status === 0 || !emitted.stderr.includes(baseExpect.node_error))
      throw new Error(`${name}: expected Node ${baseExpect.node_error}, got ${emitted.stderr}`);
  }
  if (baseExpect.node_stdout) {
    const original = run(process.execPath, [path.join(fixture, "bundle.js")]);
    const emitted = run(process.execPath, [path.join(baseOut, "entry.js")]);
    if (original.status !== 0 || emitted.status !== 0)
      throw new Error(`${name}: Node failed: ${original.stderr}\n${emitted.stderr}`);
    if (original.stdout !== baseExpect.node_stdout[0] || emitted.stdout !== baseExpect.node_stdout[1])
      throw new Error(`${name}: unexpected Node outputs: ${JSON.stringify([original.stdout, emitted.stdout])}`);
  }
  const cases = [{ name: "(unchanged)", expect: baseExpect, run: () => base }];
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
  if (baseExpect.code !== 0) {
    if (!process.argv.includes("--keep")) rmSync(work, { recursive: true, force: true });
    continue;
  }
  // every mutant of the accepted output must be rejected
  const mutants = run(process.execPath, [
    path.join(here, "mutate.mjs"),
    bundleJson,
    baseOutput,
    baseCert,
    "--count",
    "24",
  ]);
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
