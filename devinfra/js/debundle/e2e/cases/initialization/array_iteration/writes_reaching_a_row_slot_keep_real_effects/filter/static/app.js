import { consumeCallbacks } from "./peer.js";
let later = 0;
const entries = [
  { id: "a", executor: () => 0 },
  { id: "b", executor: () => 0 },
];
let observed = -1;

Array.prototype.constructor = {
  get [Symbol.species]() {
    observed = later;
    return Array;
  },
};
const result = entries.filter(() => true);

later = 42;
console.log(observed);
