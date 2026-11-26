// The ESTree Program as `lowersplit` reads it (`translation/annotated_estree.mjs`), with the parser and the scope
// analysis of this package. Excalidraw uses the same parser/scope projection.
import { acorn, eslintScope } from "./parser.mjs";
import { dumpParsed } from "./annotated_estree.mjs";
import { parseModule, topLevelStatements } from "./scope_metadata.mjs";
import { coreFromParsed, toCore, toCoreProgram } from "../model/to_core.mjs";

/** The footprint input and CoreJS program share a parse, including the
 * identity of every identifier used for opaque closure capture. */
export function splitInputs(code, options = {}) {
  const parsed = parseModule(code, acorn.parse, eslintScope);
  const units = topLevelStatements(parsed.ast);
  return { dump: dumpParsed(parsed), core: coreFromParsed(parsed, options, units), units };
}

/** The bundle `code` as the model's syntax, with the statements outside the fragment opaque (`model/to_core.mjs`). */
export const coreOf = (code, options = {}) => toCore(code, { ...options, opaque: eslintScope });

/** The program that starts at the file `entry`, with the statements outside the fragment opaque. */
export const coreProgramOf = (entry, options = {}) => toCoreProgram(entry, { ...options, opaque: eslintScope });
