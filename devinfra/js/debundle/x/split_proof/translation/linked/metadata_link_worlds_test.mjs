import assert from "node:assert/strict";
import vm from "node:vm";

const entry = "https://example.org/main.mjs";
const worker = new URL("worker.mjs", entry).href;
const reader = "function read() { return import.meta.env.MODE; } var observed = read();";
const pivot = 'var pivot = "ready";';

async function observe(moved, getter) {
  const context = vm.createContext({});
  let calls = 0;
  const main = new vm.SourceTextModule(
    (moved ? 'import { pivot } from "./worker.mjs";' + reader : reader + pivot) + "export { observed, pivot };",
    {
      context,
      identifier: entry,
      initializeImportMeta(meta, module) {
        meta.url = module.identifier;
        meta.env = getter
          ? {
              get MODE() {
                calls++;
                return module.namespace.pivot;
              },
            }
          : { MODE: undefined };
      },
    }
  );
  const other = new vm.SourceTextModule(pivot + "export { pivot };", { context, identifier: worker });
  await main.link((specifier, parent) => {
    assert.equal(new URL(specifier, parent.identifier).href, worker);
    return other;
  });
  await main.evaluate();
  assert.deepEqual(Object.keys(main.namespace), ["observed", "pivot"]);
  assert.equal(main.namespace.pivot, "ready");
  assert.equal(calls, getter ? 1 : 0);
  return main.namespace.observed;
}

assert.equal(await observe(false, false), undefined);
assert.equal(await observe(true, false), undefined);
assert.equal(await observe(false, true), undefined);
assert.equal(await observe(true, true), "ready");
