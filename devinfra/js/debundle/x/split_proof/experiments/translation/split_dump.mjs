// The statements of a bundle as `lowersplit` reads them (`lowering/dump_core.mjs`), with the parser and the scope
// analysis of this package. `model/differential.mjs` splits its programs with it.
import * as acorn from "acorn";
import * as eslintScope from "eslint-scope";
import { lowerDump } from "../lowering/dump_core.mjs";

export const dumpOf = (code) => lowerDump(code, acorn, eslintScope);
