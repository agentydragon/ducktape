// The callback runs during map(), so its read of PI must not be considered lazy.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { footprints } from "./extract.mjs";

const bundle = readFileSync(process.env.BUNDLE, "utf8");
const scale = footprints(bundle).find((statement) => statement.defs.includes("scale"));
assert.ok(scale, "missing scale statement");
assert.ok(scale.effect, "map call must be effectful");
assert.ok(scale.eager.includes("PI"), "callback's PI read must be eager");
