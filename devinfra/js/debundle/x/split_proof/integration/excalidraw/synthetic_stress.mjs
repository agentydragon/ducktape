// Frozen schedule stress cases for the pinned editor. Not authored module boundaries.
import assert from "node:assert/strict";
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";

const [input, startupFile, wideFile] = process.argv
  .slice(2)
  .map((arg) => path.resolve(process.env.JS_BINARY__EXECROOT, arg));
const { entry, modules } = JSON.parse(readFileSync(input, "utf8"));
const all = Object.values(modules)
  .flat()
  .sort((a, b) => Number(a.slice(1)) - Number(b.slice(1)));
assert.equal(all.length, 1787);
all.forEach((id, i) => assert.equal(id, "#" + i));

const editor = "packages/excalidraw/editor/";
const scene = "packages/excalidraw/scene/";
const range = (lo, hi) => Array.from({ length: hi - lo }, (_, i) => i + lo);
const startup = { [editor + "startup.mjs"]: range(5, 19) };
const retainedConstants = new Set([392, 403, 404, 409, 413, 415, 422, 423, 426, 445, 459, 462, 463]);
const wide = {
  ...startup,
  [editor + "codecs.mjs"]: range(19, 301),
  [editor + "utilities.mjs"]: range(301, 354),
  [editor + "palette-primitives.mjs"]: range(359, 367),
  [editor + "constants.mjs"]: range(385, 472).filter((i) => !retainedConstants.has(i)),
  [editor + "font-strings.mjs"]: [404, 548, 549],
  [editor + "frame-animation.mjs"]: range(550, 555),
  [editor + "collections.mjs"]: [555, 568, 569, 577, 578, 579, 580, 589, 590, 595, 596, 597],
  [scene + "coordinates.mjs"]: [559, 560],
  [scene + "scroll.mjs"]: [1337],
};
for (const [file, groups] of [
  [startupFile, startup],
  [wideFile, wide],
]) {
  const selected = Object.fromEntries(Object.entries(groups).map(([name, ids]) => [name, ids.map((i) => all[i])]));
  const moved = new Set(Object.values(selected).flat());
  writeFileSync(
    file,
    JSON.stringify({
      entry,
      modules: { [entry]: all.filter((id) => !moved.has(id)), ...selected },
      order: Object.keys(selected),
      sequential: true,
    })
  );
}
