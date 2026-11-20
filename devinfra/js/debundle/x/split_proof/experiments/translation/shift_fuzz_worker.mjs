// Execute the generated bundle or emitted tree in a fresh context. The parent
// bounds this process as well as the module's synchronous evaluation.
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";

const root = path.resolve(process.argv[2]);
const context = vm.createContext(Object.create(null), { codeGeneration: { strings: false, wasm: false } });
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
  process.stdout.write(JSON.stringify({ status: "ok", trace: context.__trace }));
} catch (e) {
  process.stdout.write(
    JSON.stringify({
      status: e?.code === "ERR_SCRIPT_EXECUTION_TIMEOUT" ? "timeout" : (e?.name ?? "Error"),
      trace: context.__trace,
    })
  );
}
