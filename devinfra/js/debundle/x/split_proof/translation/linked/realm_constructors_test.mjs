import assert from "node:assert/strict";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { writeDeployment } from "../testing/deployment_manifest.mjs";
import { probeLinkedModel } from "../testing/linked_model_probe.mjs";
import { assertPublicEmissionRefused, assertPublicRecheckRefused } from "../testing/public_refusal.mjs";
import { withSplitPreview } from "../testing/split_preview.mjs";
import {
  array,
  arrow,
  call,
  conditional,
  exportNamed,
  id,
  literal,
  prepare,
  program,
  root,
  variable,
} from "./harness.mjs";

const entry = "entry.mjs";
const owner = "factory.mjs";

const constructors = [
  { name: "Date", args: [literal(0)], observe: (v) => v.toISOString(), expected: "1970-01-01T00:00:00.000Z" },
  {
    name: "RegExp",
    args: [literal("^a$"), literal("i")],
    observe: (v) => [v.test("A"), v.test("aa")],
    expected: [true, false],
  },
  {
    name: "Set",
    args: [array([literal(1), literal(1), literal(2)])],
    observe: (v) => [...v],
    expected: [1, 2],
  },
];

for (const { name, args, observe, expected } of constructors) {
  for (const reached of [false, true]) {
    const dir = path.join(root, name + (reached ? "-eager" : "-deferred"));
    const construction = { type: "NewExpression", callee: id(name), arguments: args };
    const source = program([
      variable("const", "make", arrow([id("active")], conditional(id("active"), construction, literal(null)))),
      variable("const", "result", call(id("make"), [literal(reached)])),
      exportNamed([
        ["make", "make"],
        ["result", "result"],
      ]),
    ]);
    const files = prepare(dir, {
      source,
      provider: "",
      spec: {
        entry,
        modules: { [owner]: ["#0"], [entry]: ["#1", "#2"] },
        order: [owner],
        sequential: true,
      },
    });
    const out = path.join(dir, "out");
    const manifest = writeDeployment(files.sourceFile, files.specFile, out);
    assertPublicEmissionRefused(manifest, out);
    await withSplitPreview({ source: files.sourceFile, spec: files.specFile }, async ({ directory, entryFile }) => {
      for (const file of [files.sourceFile, entryFile]) {
        const mod = await import(pathToFileURL(file).href);
        if (reached) assert.deepEqual(observe(mod.result), expected);
        else assert.equal(mod.result, null);
        const value = mod.make(true);
        assert.deepEqual(observe(value), expected);
        assert.notEqual(mod.make(true), value);
      }
      const pair = probeLinkedModel({
        source: files.sourceFile,
        spec: files.specFile,
        directory,
        host: { profile: "browser-page" },
        observe: [["result"]],
      });
      // A single-world probe is a diagnostic, not a certificate. The profile
      // permits opaque constructors only while execution leaves them deferred.
      if (reached) {
        assert.equal(pair.status, "refused", JSON.stringify(pair));
        assert.equal(pair.reason_code, "probe_unknown");
        assert.equal(
          pair.reason,
          `original reached an unknown result during linked initialization: ambient constructor ${name} is reached`
        );
      } else {
        assert.equal(pair.status, "matched", JSON.stringify(pair));
        assert(Number.isInteger(pair.fuel) && pair.fuel > 0, JSON.stringify(pair));
        assert.deepEqual(pair.assumptions, []);
        assert.deepEqual(pair.host_refs, []);
        assert.deepEqual(pair.observed, [{ path: ["result"], original: "null", split: "null" }]);
      }
      assertPublicEmissionRefused(manifest, directory);
      assertPublicRecheckRefused(manifest, directory);
    });
  }
}
