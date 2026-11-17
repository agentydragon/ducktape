import { consumeCallbacks } from "./peer.js";
let later = 0;
const entries = [
  { id: "a", executor: () => 0 },
  { id: "b", executor: () => 0 },
];
let observed = -1;

Object.defineProperty(globalThis, "slot", {
  configurable: true,
  get() {
    return Array.prototype;
  },
  set(value) {},
});
globalThis.slot = {};
globalThis.slot.forEach = function (callback) {
  observed = later;
};
const result = entries.forEach(() => {});

later = 42;
console.log(observed);
