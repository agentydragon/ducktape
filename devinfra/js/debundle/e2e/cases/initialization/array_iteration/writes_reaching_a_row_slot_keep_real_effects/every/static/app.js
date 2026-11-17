import { consumeCallbacks } from "./peer.js";
let later = 0;
const entries = [
  { id: "a", executor: () => 0 },
  { id: "b", executor: () => 0 },
];
let observed = -1;

Object.defineProperty(Array.prototype, "every", {
  value() {
    observed = later;
    return true;
  },
  configurable: true,
});
const result = entries.every(() => true);

later = 42;
console.log(observed);
