// The generator of programs that can reach any part of the language: the families and `generateProgram`.
import * as classes from "./classes.mjs";
import { build, renderProgram } from "./core.mjs";
import * as expressions from "./expressions.mjs";
import * as functions from "./functions.mjs";
import * as patterns from "./patterns.mjs";
import * as statements from "./statements.mjs";

/** The families in the order they are installed; each installs productions of expressions and statements and makers of units. */
export const FAMILIES = { expressions, statements, functions, classes, patterns };

/**
 * One program for `seed`: `{ units: [{ tmpl, src, defs, uses }], exports }`, the shape the fuzzer's splitter takes.
 * `families` names the families to install (all by default), `units` the size to aim for, and `avoid` the constructs that are
 * left out (`array-constructor`: `new Array(n)`, a finding in `debug/generator_findings`), so that a known finding does not
 * hide the next.
 */
export function generateProgram(seed, { families = Object.keys(FAMILIES), units, avoid } = {}) {
  return build(
    seed,
    families.map((id) => FAMILIES[id].install),
    { units, avoid }
  );
}

export { renderProgram };
