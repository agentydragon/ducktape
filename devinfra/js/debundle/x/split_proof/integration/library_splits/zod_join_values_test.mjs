import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { cpSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { acorn, dump } from "../../translation/parser.mjs";
import { topLevelStatements } from "../../translation/scope_metadata.mjs";
import { split } from "../../translation/testing/split.mjs";

const [file] = process.argv.slice(2);
const dir = process.env.TEST_TMPDIR;
const artifacts = process.env.TEST_UNDECLARED_OUTPUTS_DIR;
const sha = (bytes) => createHash("sha256").update(bytes).digest("hex");
const write = (name, contents) => {
  const dest = path.join(dir, name);
  writeFileSync(dest, contents);
  return dest;
};
const load = (p) => import(pathToFileURL(path.resolve(p)).href);
const code = readFileSync(file, "utf8");
const units = topLevelStatements(acorn.parse(code, { sourceType: "module", ecmaVersion: "latest" }));
assert.equal(sha(code), "5d67ee00c72ddfbaa8d82f88913330687b364e6767faf5065538238d21495538");
assert.equal(units.length, 1072);
function original(i, name, digest) {
  const { node, start, end } = units[i];
  assert.equal(node.id.name, name);
  const source = code.slice(start, end);
  assert.equal(sha(source), digest);
  return source;
}
const mapper = original(17, "joinValues", "8bf32110513942f1e127d91fc73d59c9ea1fc1dc4d7ccf4ad84935f242d611aa");
const formatter = original(
  54,
  "stringifyPrimitive",
  "6da4d7f94ef6f9b9b636936eeffd702d1140a744db8efc19a96bee7c28f4429f"
);
const entry = "entry.mjs";
const helper = "format.mjs";
const placement = (moved, retained) => ({
  entry,
  modules: { [helper]: moved, [entry]: retained },
  order: [helper],
  sequential: true,
});
const spec = write("spec.json", JSON.stringify(placement(["#0", "#1"], ["#2"])));
const projection = mapper + "\n" + formatter + "\nexport { joinValues };\n";
const source = write("projection.mjs", projection);
const out = path.join(dir, "compiled");
const request = { source, spec, out, require: "all-outcomes" };
const receipt = split(request);
if (process.argv.includes("--before")) {
  assert.equal(receipt.reasonCode, "no_constructed_rule", receipt.log);
  assert.equal(receipt.reason, "expected an ordinary one-parameter return predicate", receipt.log);
  console.log(JSON.stringify(receipt.verdict));
} else {
  assert.equal(receipt.status, "certified", receipt.log);
  assert.equal(receipt.scope, "indexed-exported-function", receipt.log);
  assert.equal(receipt.guarantee, "all-outcomes", receipt.log);
  assert.equal(receipt.verdict.strategy, "compiled-constructor");
  assert.deepEqual(receipt.files, [entry, helper]);
  const disk = split({ ...request, checkOutput: true });
  assert.deepEqual(disk.verdict, receipt.verdict);
  const fromBundle = (await load(file)).z.util.joinValues;
  const flat = (await load(source)).joinValues;
  const emitted = await load(path.join(out, entry));
  assert.deepEqual(Object.keys(emitted), ["joinValues"]);
  const joined = emitted.joinValues;
  assert.equal(joined, (await load(path.join(out, helper))).joinValues);
  assert.equal(joined.length, 1);
  const values = [1, "s", 3n];
  for (const [args, want] of [
    [[values], '1|"s"|3n'],
    [[values, undefined], '1|"s"|3n'],
    [[values, null], '1null"s"null3n'],
    [[values, ""], '1"s"3n'],
  ]) {
    for (const join of [fromBundle, flat, joined]) assert.equal(join(...args), want);
  }

  // Ordinary method lookup and receivers, with no dependence on an array method.
  function exercise(join) {
    const trace = [];
    const result = {
      join(separator) {
        assert.equal(this, result);
        trace.push(separator);
        return "result";
      },
    };
    const input = {
      get map() {
        trace.push("get map");
        return function (callback) {
          assert.equal(this, input);
          trace.push(callback(3n));
          return result;
        };
      },
    };
    assert.equal(join(input, null), "result");
    return trace;
  }
  for (const join of [fromBundle, flat, joined]) assert.deepEqual(exercise(join), ["get map", "3n", null]);

  const observer = `
import { joinValues } from "./entry.mjs";
const mapped = { join(sep) { console.log(this === mapped); console.log(sep); } };
const input = { get map() {
  console.log("get map");
  return function (convert) {
    console.log(this === input);
    console.log(convert("text"));
    return mapped;
  };
} };
joinValues(input);
joinValues(input, null);
`;
  function model(providers, code = observer) {
    const input = write("model.jsonl", JSON.stringify({ ...dump(code), entry: "consumer.mjs", providers }) + "\n");
    return JSON.parse(execFileSync(process.env.COREJS, ["--ast", input], { encoding: "utf8" }));
  }
  const before = { [entry]: dump(projection) };
  const after = Object.fromEntries(
    receipt.files.map((name) => [name, dump(readFileSync(path.join(out, name), "utf8"))])
  );
  for (const providers of [before, after]) {
    const observed = model(providers);
    const prefix = ["get map", "true", '"text"', "true"];
    assert.deepEqual(
      [observed.end, observed.out],
      ["normal", [...prefix, "|", ...prefix, "null"]],
      JSON.stringify(observed)
    );
    const array = model(providers, 'import { joinValues } from "./entry.mjs"; joinValues([1]);');
    assert.equal(array.end, "unknown", JSON.stringify(array));
    assert.match(array.detail, /Array\.prototype\.map/);
  }

  const tampered = path.join(dir, "tampered");
  cpSync(out, tampered, { recursive: true });
  const tamperedFile = path.join(tampered, helper);
  const text = readFileSync(tamperedFile, "utf8");
  const edited = text.replace('"|"', '"/"');
  assert.notEqual(edited, text);
  writeFileSync(tamperedFile, edited);
  assert.equal((await load(path.join(tampered, entry))).joinValues(values), '1/"s"/3n');
  const badDisk = split({ ...request, out: tampered, checkOutput: true });
  assert.equal(badDisk.status, "refused", badDisk.log);
  const badSpec = write("wrong-owner.json", JSON.stringify(placement(["#0"], ["#1", "#2"])));
  const badOwner = split({ ...request, spec: badSpec, allowExistingOut: true });
  assert.equal(badOwner.status, "refused", badOwner.log);
  assert.equal(badOwner.reason, "the two hoists must share the helper and publish only the original mapper");

  // A separate fixture exercises ordering and TDZ without changing the real case.
  const defaults = `function joinValues(hooks, input = hooks.input, separator = hooks.separator) {
  return input.map((item) => stringifyPrimitive(item)).join(separator);
}
`;
  const tdz = defaults.replace("input = hooks.input", "input = separator");
  for (const [kind, data] of [
    ["ordered", defaults],
    ["tdz", tdz],
  ]) {
    const testSource = write(kind + ".mjs", data + formatter + "\nexport { joinValues };\n");
    const testOut = path.join(dir, kind);
    const testReq = { ...request, source: testSource, out: testOut };
    const testReceipt = split(testReq);
    assert.equal(testReceipt.status, "certified", testReceipt.log);
    assert.deepEqual(split({ ...testReq, checkOutput: true }).verdict, testReceipt.verdict);
    for (const mod of [await load(testSource), await load(path.join(testOut, entry))]) {
      const trace = [];
      const hooks = {
        get input() {
          trace.push("input");
          return values;
        },
        get separator() {
          trace.push("separator");
          return ":";
        },
      };
      if (kind === "ordered") {
        assert.equal(mod.joinValues(hooks), '1:"s":3n');
        assert.deepEqual(trace, ["input", "separator"]);
        assert.equal(mod.joinValues(hooks, values, null), '1null"s"null3n');
        assert.deepEqual(trace, ["input", "separator"]);
      } else {
        assert.throws(() => mod.joinValues(hooks, undefined, ":"), ReferenceError);
        assert.deepEqual(trace, []);
      }
    }
  }

  const whole = write(
    "whole.json",
    JSON.stringify(
      placement(
        ["#17", "#54"],
        units.flatMap((_, i) => (i === 17 || i === 54 ? [] : ["#" + i]))
      )
    )
  );
  const bundle = split({ source: file, spec: whole, out: path.join(dir, "whole"), require: "all-outcomes" });
  assert.equal(bundle.reasonCode, "no_constructed_rule", bundle.log);
  writeFileSync(
    path.join(artifacts, "join-values.json"),
    JSON.stringify(
      {
        original: { sha256: sha(code), ordinals: [17, 54] },
        projection: {
          sha256: sha(projection),
          bytes: Buffer.byteLength(projection),
          receipt: receipt.verdict,
          disk: disk.verdict,
        },
        negative: { tampered: badDisk.verdict, wrongOwner: badOwner.verdict },
        whole: bundle.verdict,
      },
      null,
      2
    ) + "\n"
  );
}
