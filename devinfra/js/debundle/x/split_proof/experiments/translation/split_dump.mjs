// The statements of a bundle as `lowersplit` reads them (`lowering/dump_core.mjs`), with the parser and the scope
// analysis of this package. `model/differential.mjs` splits its programs with it.
import * as acorn from "acorn";
import * as eslintScope from "eslint-scope";
import { dumpParsed, lowerDump } from "../lowering/dump_core.mjs";
import { parseModule } from "./scope_metadata.mjs";
import { coreFromParsed, toCore, toCoreProgram } from "../model/to_core.mjs";

export const dumpOf = (code) => lowerDump(code, acorn, eslintScope);

/** The footprint input and CoreJS program share a parse, including the
 * identity of every identifier used for opaque closure capture. */
export function splitInputs(code, options = {}) {
  const parsed = parseModule(code, acorn.parse, eslintScope);
  return { dump: dumpParsed(parsed), core: coreFromParsed(parsed, options) };
}

/** The bundle `code` as the model's syntax, with the statements outside the fragment opaque (`model/to_core.mjs`). */
export const coreOf = (code, options = {}) => toCore(code, { ...options, opaque: eslintScope });

/** The program that starts at the file `entry`, with the statements outside the fragment opaque. */
export const coreProgramOf = (entry, options = {}) => toCoreProgram(entry, { ...options, opaque: eslintScope });
