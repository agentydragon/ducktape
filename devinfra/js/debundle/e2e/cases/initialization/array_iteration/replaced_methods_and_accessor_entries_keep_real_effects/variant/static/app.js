import { consumeCallbacks } from "./peer.js";
let later = 0;
const entries = [
  { id: "a", executor: () => 0 },
  { id: "b", executor: () => 0 },
];
let observed = -1;

entries[Symbol.iterator] = function* () {
  observed = later;
  yield entries[0];
};
const result = [...entries];

later = 42;
console.log(observed);
