// The statements of a bundle as `lowersplit` reads them (`lowering/dump_core.mjs`), with the parser and the scope
// analysis of this package. `model/differential.mjs` splits its programs with it.
import * as acorn from "acorn";
import * as eslintScope from "eslint-scope";
import { lowerDump } from "../lowering/dump_core.mjs";
import { toCore, toCoreProgram } from "../model/to_core.mjs";

export const dumpOf = (code) => lowerDump(code, acorn, eslintScope);

/** The bundle `code` as the model's syntax, with the statements outside the fragment opaque (`model/to_core.mjs`). */
export const coreOf = (code, options = {}) => toCore(code, { ...options, opaque: eslintScope });

/** The program that starts at the file `entry`, with the statements outside the fragment opaque. */
export const coreProgramOf = (entry, options = {}) => toCoreProgram(entry, { ...options, opaque: eslintScope });
