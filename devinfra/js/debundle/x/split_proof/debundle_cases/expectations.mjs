// Account for authored corpus observations separately from proof admission.
import path from "node:path";
import { pathToFileURL } from "node:url";

export function expectationLedger(accept, status = "unchecked") {
  const list = [
    { assertion: "stdout:original", status },
    { assertion: "stdout:emitted", status },
  ];
  for (const kind of ["exports", "probes", "emitted_probes", "source", "runtime_files"]) {
    const keys = Object.keys(accept[kind] ?? {});
    if (!keys.length) list.push({ assertion: kind, status: "inapplicable" });
    else for (const key of keys) list.push({ assertion: `${kind}:${key}`, status });
  }
  list.push({ assertion: "check_all_js", status: accept.check_all_js ? status : "inapplicable" });
  list.push({ assertion: "entry", status: accept.entry === undefined ? "inapplicable" : status });
  const known = new Set([
    "stdout",
    "original_stdout",
    "entry",
    "exports",
    "probes",
    "emitted_probes",
    "source",
    "runtime_files",
    "check_all_js",
  ]);
  for (const key of Object.keys(accept)) if (!known.has(key)) list.push({ assertion: `unrepresented:${key}`, status });
  return list;
}

const set = (ledger, assertion, status, detail) => {
  const row = ledger.find((item) => item.assertion === assertion);
  if (!row) throw new Error(`unknown expectation ${assertion}`);
  Object.assign(row, { status }, detail === undefined ? {} : { detail });
};
const ok = (result) => !result.failure && result.code === 0;
const moduleUrl = (file) => pathToFileURL(file).href;
const safeRelative = (name) =>
  !path.isAbsolute(name) && name.split("/").every((part) => part && part !== "." && part !== "..");

/** Inspect only published, independently rechecked files. Unmapped corpus paths stay unchecked. */
export function observeExpectations({ accept, stage, chunkFile, output, entry, run, nodeRun }) {
  const ledger = expectationLedger(accept);
  const original = path.resolve(stage, chunkFile);
  const emitted = path.resolve(output, entry);
  for (const [label, file, expected] of [
    ["original", original, accept.original_stdout ?? accept.stdout],
    ["emitted", emitted, accept.stdout],
  ]) {
    const result = nodeRun(label, file, stage);
    set(ledger, `stdout:${label}`, ok(result) && result.stdout === expected && !result.stderr ? "pass" : "fail");
  }
  // The adapter uses a flat synthetic deployment. Only the authored chunk entry has an unambiguous route.
  const chunk = chunkFile.replace(/\.js$/, "");
  if (accept.entry === chunk) set(ledger, "entry", "pass");
  for (const [name, expectation] of Object.entries(accept.exports ?? {})) {
    if (name !== `${chunk}/entry.js`) continue;
    const script = `const m = await import(process.argv[1]); console.log('NAMESPACE ' + JSON.stringify(Object.keys(m)));`;
    const names = (file) => {
      const result = run(process.execPath, ["--input-type=module", "-e", script, moduleUrl(file)], { cwd: stage });
      const line = result.stdout.split("\n").findLast((s) => s.startsWith("NAMESPACE "));
      return ok(result) && line ? JSON.parse(line.slice("NAMESPACE ".length)) : null;
    };
    const before = names(original),
      after = names(emitted);
    const passes =
      after &&
      before &&
      (expectation.include ?? []).every((n) => after.includes(n)) &&
      (expectation.exclude ?? []).every((n) => !after.includes(n));
    set(ledger, `exports:${name}`, passes ? "pass" : "fail", { original: before, emitted: after });
  }
  for (const [name, expectation] of Object.entries(accept.probes ?? {})) {
    if (!safeRelative(name)) throw new Error(`invalid probe path: ${name}`);
    const script = `const {default: probe} = await import(process.argv[1]); await probe(await import(process.argv[2]));`;
    const results = [original, emitted].map((file) =>
      run(process.execPath, ["--input-type=module", "-e", script, moduleUrl(path.join(stage, name)), moduleUrl(file)], {
        cwd: stage,
      })
    );
    set(
      ledger,
      `probes:${name}`,
      results.every((r) => ok(r) && r.stdout === expectation.stdout && !r.stderr) ? "pass" : "fail"
    );
  }
  return ledger;
}
