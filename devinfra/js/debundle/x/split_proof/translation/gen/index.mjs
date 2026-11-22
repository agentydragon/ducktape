// The generator of programs that can reach any part of the language: the families and `generateProgram`.
import * as classes from "./classes.mjs";
import { build, renderProgram } from "./core.mjs";
import * as expressions from "./expressions.mjs";
import * as functions from "./functions.mjs";
import * as modules from "./modules.mjs";
import * as patterns from "./patterns.mjs";
import * as protocols from "./protocols.mjs";
import * as statements from "./statements.mjs";
import * as unmodeled from "./unmodeled.mjs";

/** The families in the order they are installed; each installs productions of expressions and statements and makers of units. */
export const FAMILIES = { expressions, statements, functions, classes, patterns, protocols, modules, unmodeled };

/**
 * One program for `seed`: `{ units: [{ tmpl, src, defs, uses }], exports }`, the shape the fuzzer's splitter takes.
 * `families` names the families to install (all by default) and `units` the size to aim for.
 */
export function generateProgram(seed, { families = Object.keys(FAMILIES), units } = {}) {
  return build(
    seed,
    families.map((id) => FAMILIES[id].install),
    { units }
  );
}

export { renderProgram };
