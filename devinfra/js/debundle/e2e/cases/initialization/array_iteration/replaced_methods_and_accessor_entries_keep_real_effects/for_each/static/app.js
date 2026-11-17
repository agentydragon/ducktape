import { consumeCallbacks } from "./peer.js";
let later = 0;
const entries = [
  { id: "a", executor: () => 0 },
  { id: "b", executor: () => 0 },
];
let observed = -1;

Object.defineProperty(entries[0], "id", {
  get() {
    observed = later;
    return "a";
  },
});
const result = entries.forEach((entry) => {
  entry.id;
});

later = 42;
console.log(observed);
