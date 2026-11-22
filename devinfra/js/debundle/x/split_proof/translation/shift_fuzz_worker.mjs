// Execute the generated bundle or emitted tree in a fresh context. The parent
// bounds this process as well as the module's synchronous evaluation.
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";

const root = path.resolve(process.argv[2]);
const context = vm.createContext(Object.create(null), { codeGeneration: { strings: false, wasm: false } });
const marker = process.argv[4];
const modernSeen = new Set();
if (marker)
  Object.defineProperty(context, marker, {
    value: (kind, value) => {
      modernSeen.add(kind);
      return value;
    },
    enumerable: false,
    writable: false,
    configurable: false,
  });
const result = (status) => ({ status, trace: context.__trace, ...(marker ? { modern: [...modernSeen].sort() } : {}) });
vm.runInContext(
  `
globalThis.__trace = [];
globalThis.console = { log(...args) { __trace.push(args.map(String).join(" ")); } };
Math.random = () => 0.5;
`,
  context
);
const modules = new Map();
function load(file) {
  const full = path.resolve(root, file);
  if (!full.startsWith(root + path.sep)) throw new Error("module outside generated tree");
  if (!modules.has(full))
    modules.set(full, new vm.SourceTextModule(fs.readFileSync(full, "utf8"), { context, identifier: full }));
  return modules.get(full);
}
try {
  const entry = load(process.argv[3]);
  await entry.link((name, parent) => {
    if (!name.startsWith(".")) throw new Error("external import");
    return load(path.relative(root, path.resolve(path.dirname(parent.identifier), name)));
  });
  await entry.evaluate({ timeout: 100 });
  process.stdout.write(JSON.stringify(result("ok")));
} catch (e) {
  process.stdout.write(
    JSON.stringify(result(e?.code === "ERR_SCRIPT_EXECUTION_TIMEOUT" ? "timeout" : (e?.name ?? "Error")))
  );
}
