import { strict as assert } from "node:assert";
import vm from "node:vm";
import * as acorn from "acorn";
import { composeModernExpressions } from "./modern_syntax.mjs";

const source = "const a=1,b=2,c=3; const obj={7:8}; globalThis.answer=[a,b,c,obj[7]];";
const parsed = acorn.parse(source, { ecmaVersion: "latest", sourceType: "module" });
let i = 0;
const transformed = composeModernExpressions(source, parsed, () => [0, 0.4, 0.8][i++ % 3], "__testMarker");
const reparsed = acorn.parse(transformed.source, { ecmaVersion: "latest", sourceType: "module" });
assert.equal(reparsed.body[1].declarations[0].init.properties[0].key.value, 7);
const trace = [];
const context = { __testMarker: (kind, value) => { trace.push(kind); return value; } };
vm.runInNewContext(transformed.source, context, { timeout: 100 });
assert.deepEqual(Array.from(context.answer), [1, 2, 3, 8]);
for (const kind of ["nullish", "optional", "field"]) assert(trace.includes(kind), `${kind} not executed`);
