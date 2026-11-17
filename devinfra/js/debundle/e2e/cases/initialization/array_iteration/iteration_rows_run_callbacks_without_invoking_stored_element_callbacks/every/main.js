let later = 0;

const entries = [
  { id: "a", size: 1, executor: () => later },
  { id: "b", size: 2, executor: () => later },
];
const result = entries.every((entry) => entry.size > 0);

later = 42;
console.log(JSON.stringify(result), entries[0].executor());
