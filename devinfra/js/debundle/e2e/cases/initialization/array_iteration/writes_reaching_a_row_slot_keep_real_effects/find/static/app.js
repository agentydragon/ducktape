import { consumeCallbacks } from "./peer.js";
let later = 0;
const entries = [
  { id: "a", executor: () => 0 },
  { id: "b", executor: () => 0 },
];
let observed = -1;
const key = ["fi", "nd"].join("");
Array.prototype[key] = function () {
  observed = later;
};
const result = entries.find(() => false);

later = 42;
console.log(observed);
