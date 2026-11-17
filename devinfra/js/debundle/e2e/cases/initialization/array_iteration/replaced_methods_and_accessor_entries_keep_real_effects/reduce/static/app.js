import { consumeCallbacks } from "./peer.js";
let later = 0;
const entries = [
  { id: "a", executor: () => 0 },
  { id: "b", executor: () => 0 },
];
let observed = -1;

entries.reduce = function () {
  observed = later;
};
const result = entries.reduce((total) => total, 0);

later = 42;
console.log(observed);
