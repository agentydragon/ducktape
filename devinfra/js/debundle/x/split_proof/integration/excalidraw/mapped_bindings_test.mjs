import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { mappedBindings } from "./mapped_bindings.mjs";

const dir = process.env.EXCALIDRAW_PACKAGE;
const file = path.join(dir, "dist/dev/chunk-4FTI6OG3.js");
const rows = mappedBindings(readFileSync(file, "utf8"), JSON.parse(readFileSync(file + ".map", "utf8")));
const heading = rows.filter((row) => row.source === "../../element/heading.ts");
assert.deepEqual(
  heading.map((row) => row.binding),
  [
    "HEADING_RIGHT",
    "HEADING_DOWN",
    "HEADING_LEFT",
    "HEADING_UP",
    "headingForDiamond",
    "vectorToHeading",
    "headingForPoint",
    "headingForPointIsHorizontal",
    "compareHeading",
    "headingIsHorizontal",
    "headingForPointFromElement",
    "flipHeading",
  ]
);
console.log(
  JSON.stringify({ mappedBindings: rows.length, sources: new Set(rows.map((row) => row.source)).size, heading })
);
