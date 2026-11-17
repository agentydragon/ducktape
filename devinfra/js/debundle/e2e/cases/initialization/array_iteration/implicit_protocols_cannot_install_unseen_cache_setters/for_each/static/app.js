import { consumeCallbacks } from "./peer.js";
let later = 0;
const entries = [
  { id: "a", executor: () => 0 },
  { id: "b", executor: () => 0 },
];
let observed = -1;
const lookup = {};
const key = {
  toString() {
    Object.defineProperty(lookup, "a", {
      set(value) {
        observed = later;
      },
      configurable: true,
    });
    return "a";
  },
};

const result = entries.forEach((entry) => {
  lookup[`${key}`] = entry.id;
});

later = 42;
console.log(observed);
