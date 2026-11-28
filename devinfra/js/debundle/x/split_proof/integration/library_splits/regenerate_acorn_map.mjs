// Run after building the Acorn release with Rollup's --sourcemap option.
import assert from "node:assert/strict";
import { readFileSync, writeFileSync } from "node:fs";
import { gzipSync } from "node:zlib";
import { mapIntervals, sha256 } from "./source_ownership.mjs";

const [publishedFile, rebuiltFile, rollupMap, out] = process.argv.slice(2);
assert(out, "usage: regenerate_acorn_map <published JS> <rebuilt JS> <Rollup map> <output .json.gz>");
const published = readFileSync(publishedFile);
const rebuilt = readFileSync(rebuiltFile);
const trailer = Buffer.from("//# sourceMappingURL=acorn.mjs.map\n");
assert.deepEqual(rebuilt.subarray(-trailer.length), trailer);
assert.deepEqual(rebuilt.subarray(0, -trailer.length), published);
const map = JSON.parse(readFileSync(rollupMap));
mapIntervals(published.toString("utf8"), map);
const sources = map.sources.map((file) => {
  assert(file.startsWith("../src/"), file);
  return "acorn/" + file.slice(3);
});
// Only retain the coordinates, not a second copy of the upstream sources.
writeFileSync(
  out,
  gzipSync(
    JSON.stringify({
      distSha256: sha256(published),
      map: {
        version: map.version,
        sources,
        mappings: map.mappings,
      },
    }) + "\n",
    { level: 9 }
  )
);
