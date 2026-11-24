// The marker renderer preserves program spelling, including names visible through Function.name.
import { strict as assert } from "node:assert";
import { runInNewContext } from "node:vm";
import { seededRandom } from "../seeded_random.mjs";
import { generateCorpusBundle, literal, local, renderBundle, split, top } from "./fuzz_support.mjs";

const bundle = {
  units: [
    {
      tmpl: "namedFunction",
      defs: ["visible"],
      uses: [],
      src: `function ${top("visible")}() { return "${literal("visible")}"; }`,
    },
    {
      tmpl: "nameProbe",
      defs: [],
      uses: ["visible"],
      src: `console.log(${top("visible")}.name, ((${local("value")}) => ${local("value")})(${top("visible")}()));`,
    },
  ],
  exports: [],
};
const hasMarker = (source) =>
  ["\u0001", "\u0002", "\u0003", "\u0004", "\u0005", "\u0006"].some((marker) => source.includes(marker));
const rendered = renderBundle(bundle);
assert.equal(
  rendered,
  'function visible() { return "visible"; }\nconsole.log(visible.name, ((value) => value)(visible()));\n'
);
assert.equal(hasMarker(rendered), false);
const observed = [];
runInNewContext(rendered, { console: { log: (...values) => observed.push(values) } });
assert.deepEqual(observed, [["visible", "visible"]]);

const tree = split(bundle, seededRandom(7), { place: [1, 0], renameLocals: true });
assert.equal(tree.modules, 2);
assert.match(tree.files["m1.js"], /function visible\(\)/);
assert.match(tree.files["m1.js"], /export\s*\{\s*visible\s+as\s+x_visible\s*\}/);
assert.match(tree.files["entry.js"], /import\s*\{\s*x_visible\s+as\s+visible\s*\}\s*from\s*["']\.\/m1\.js["']/);
assert.match(tree.files["entry.js"], /\(\(l_value\)\s*=>\s*l_value\)/);
assert.equal(hasMarker(Object.values(tree.files).join("\n")), false);

const plan = renderBundle(
  generateCorpusBundle({ seed: 17, round: 3, astCorpus: false, callbackCorpus: false, fullCorpus: false, leaks: false })
);
const replay = renderBundle(
  generateCorpusBundle({ seed: 17, round: 3, astCorpus: false, callbackCorpus: false, fullCorpus: false, leaks: false })
);
assert.equal(plan, replay, "template corpus must be reproducible from its seed and round");
