// node prefix.mjs <bundle.json> <n> > prefix.json
import { readFileSync } from "node:fs";
const [file, n] = process.argv.slice(2);
const d = JSON.parse(readFileSync(file, "utf8"));
process.stdout.write(JSON.stringify({ statements: d.statements.slice(0, Number(n)) }));
