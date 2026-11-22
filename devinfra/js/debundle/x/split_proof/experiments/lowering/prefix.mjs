// node prefix.mjs <bundle.json> <n> > prefix.json
import { readFileSync } from "node:fs";
import * as acorn from "acorn";
import * as eslintScope from "eslint-scope";
import { topLevelStatements } from "../translation/scope_metadata.mjs";
import { lowerDump } from "./dump_core.mjs";

const [file, n] = process.argv.slice(2);
const program = JSON.parse(readFileSync(file, "utf8"));
const last = topLevelStatements(program, program.sourceText).slice(0, Number(n)).at(-1);
const prefix = program.sourceText.slice(0, last?.end ?? 0);
process.stdout.write(JSON.stringify(lowerDump(prefix, acorn, eslintScope)));
