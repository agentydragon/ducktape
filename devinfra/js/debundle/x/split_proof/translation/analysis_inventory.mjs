// What the effect analysis models, read from its Lean sources: the ESTree node types its interpreter has an arm for
// (`eval`, `exec`, `evalClass`, `bindPattern`; any other type is a give-up, which makes its unit touch the world), the
// rows of the table of standard functions (`Intrinsics.lean`) and the members the standard objects have
// (`IntrinsicMembers.lean`). A text scan: it stays right while the sources keep the shapes it reads, and
// `coverage_audit_test.mjs` runs it on small samples of those shapes.
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

/** The interpreter functions whose `match` arms name node types. */
export const INTERPRETER_FUNCTIONS = ["eval", "evalClass", "bindPattern", "exec"];

const here = path.dirname(fileURLToPath(import.meta.url));
export const DEFAULT_LEAN_DIR = path.join(here, "../lean/Experiments");

/** The text of `partial def <name>` up to the next top-level definition of the file. */
function definitionText(source, name) {
  const start = source.search(new RegExp(`^(partial )?def ${name}\\b`, "m"));
  if (start < 0) throw new Error(`no definition of ${name}`);
  const rest = source.slice(start + 1);
  const next = rest.search(/^(partial def|def|end|mutual|structure|inductive|namespace|unsafe def|@\[)\b/m);
  return next < 0 ? rest : rest.slice(0, next);
}

/** The node types a function has an arm for: `| "A" | "B" =>`. */
export function nodeTypeArms(source, name) {
  const arms = new Set();
  for (const m of definitionText(source, name).matchAll(/^\s*\|\s*((?:"[A-Za-z]+"\s*\|?\s*)+)=>/gm))
    for (const t of m[1].matchAll(/"([A-Za-z]+)"/g)) arms.add(t[1]);
  return arms;
}

/** Keys of the `table` of `Intrinsics.lean`, with the rows of `iteratorHelperRows` expanded. */
export function tableKeys(source) {
  const table = source.slice(source.search(/^def table\b/m));
  const end = table.search(/\] : List \(String × Summary\)/);
  if (end < 0) throw new Error("the end of `table` was not found");
  const keys = [...table.slice(0, end).matchAll(/\("([^"]+)",\s*\{/g)].map((m) => m[1]);
  const helpers = source.match(
    /def iteratorHelperRows[\s\S]*?\[([^\]]*)\]\.flatMap fun m =>\s*\[([^\]]*)\]\.map fun c/
  );
  if (!helpers) throw new Error("`iteratorHelperRows` was not found");
  const strings = (list) => [...list.matchAll(/"([^"]+)"/g)].map((m) => m[1]);
  for (const c of strings(helpers[2])) for (const m of strings(helpers[1])) keys.push(`${c}.prototype.${m}`);
  return keys;
}

/** `memberPaths`: `Path.member` for every own property of the standard objects. */
export function memberPaths(source) {
  const start = source.indexOf("def memberPaths");
  if (start < 0) throw new Error("no `memberPaths`");
  const list = source.slice(source.indexOf("[", start), source.indexOf("\n]", start));
  return [...list.matchAll(/^\s*"([^"]+)",?$/gm)].map((m) => m[1]);
}

/** A `List String` definition such as `intrinsicGlobals`. */
export function stringList(source, name) {
  const start = source.search(new RegExp(`def ${name}\\b`));
  if (start < 0) throw new Error(`no definition of ${name}`);
  const open = source.indexOf("[", start);
  return [...source.slice(open, source.indexOf("]", open)).matchAll(/"([^"]+)"/g)].map((m) => m[1]);
}

export function readAnalysisInventory(leanDir = DEFAULT_LEAN_DIR) {
  const read = (file) => readFileSync(path.join(leanDir, file), "utf8");
  const effects = read("Effects.lean");
  const intrinsics = read("Intrinsics.lean");
  return {
    arms: Object.fromEntries(INTERPRETER_FUNCTIONS.map((f) => [f, nodeTypeArms(effects, f)])),
    tableKeys: tableKeys(intrinsics),
    memberPaths: memberPaths(read("IntrinsicMembers.lean")),
    intrinsicGlobals: stringList(intrinsics, "intrinsicGlobals"),
  };
}
