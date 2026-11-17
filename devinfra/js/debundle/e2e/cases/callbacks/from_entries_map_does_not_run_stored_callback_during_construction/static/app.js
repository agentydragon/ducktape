let invocations = 0;
let later = 0;
const read = () => {
  invocations++;
  return later;
};
globalThis.saved = read;
const entries = [{ name: "read", run: read }];
const registry = Object.fromEntries(entries.map((entry) => [entry.name, entry]));
const duringConstruction = invocations;
later = 42;
const result = registry.read.run();
console.log(duringConstruction, invocations, result);
export { entries, registry, duringConstruction, later, result };
