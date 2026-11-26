// A bundle in the CoreJS model, as `split_preview --corejs` reads it to name the model diagnostic for a split.
//
//   node core_program.mjs <bundle.mjs> > program.json
//
// One module: its statements in the model's syntax or opaque (`model/to_core.mjs`, option `opaque`), each with the
// identity `lowersplit` gives it. The bundle's own `import`s are the world's (option `external`). A statement that no
// statement of the model can stand for, `import()` or `import.meta`, is opaque all the same (option `census`) and
// listed on stderr: `lowersplit` decides whether that reading is faithful for the split.
import { readFileSync } from "node:fs";
import { coreOf } from "./split_dump.mjs";

const [file] = process.argv.slice(2);
const core = coreOf(readFileSync(file, "utf8"), { external: true, census: true });
for (const { id, why } of core.refused) console.error(`statement ${id}: opaque, although ${why}`);
console.error(
  `${core.numbered} statements: ${core.units - core.opaque.length} in the model's syntax, ${core.opaque.length} opaque`
);
const { tokens, ids, exports } = core;
console.log(JSON.stringify({ entry: "main", modules: [{ name: "main", imports: [], exports, ids, tokens }] }));
