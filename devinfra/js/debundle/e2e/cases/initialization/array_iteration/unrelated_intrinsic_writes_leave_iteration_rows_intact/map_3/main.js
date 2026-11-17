let later = 0;
Object.defineProperty(Object.prototype, "unrelated", { value: 1, configurable: true });
const entries = [
  { id: "a", size: 1, executor: () => later },
  { id: "b", size: 2, executor: () => later },
];
const result = entries.map((entry) => entry.size);

later = 42;
console.log(JSON.stringify(result), entries[0].executor());
