import { consumeCallbacks } from "./peer.js";
let later = 0;
const entries = [
  { id: "a", executor: () => 0 },
  { id: "b", executor: () => 0 },
];
let observed = -1;
const filtered = entries.filter((entry) => entry.id);
const C = filtered.constructor;
Object.defineProperty(C, Symbol.species, {
  get() {
    observed = later;
    return Array;
  },
});
const result = filtered.map((entry) => entry);

later = 42;
console.log(observed);
