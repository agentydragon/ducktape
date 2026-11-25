// node prefix.mjs <bundle.json> <n> > prefix.json
import { readFileSync } from "node:fs";
import { topLevelStatements } from "../translation/scope_metadata.mjs";
import { dump } from "../translation/parser.mjs";

const [file, n] = process.argv.slice(2);
const program = JSON.parse(readFileSync(file, "utf8"));
const last = topLevelStatements(program).slice(0, Number(n)).at(-1);
const prefix = program.sourceText.slice(0, last?.end ?? 0);
process.stdout.write(JSON.stringify(dump(prefix)));
