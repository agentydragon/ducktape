import { consumeCallbacks } from "./peer.js";
let later = 0;
const entries = [
  { id: "a", executor: () => 0 },
  { id: "b", executor: () => 0 },
];
let observed = -1;
const C = entries.constructor;
Object.defineProperty(C, Symbol.species, {
  get() {
    observed = later;
    return Array;
  },
});
const result = entries.map((entry) => entry);

later = 42;
console.log(observed);
