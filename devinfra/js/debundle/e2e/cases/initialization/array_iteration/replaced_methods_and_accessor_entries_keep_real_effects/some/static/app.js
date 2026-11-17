import { consumeCallbacks } from "./peer.js";
let later = 0;
const entries = [
  { id: "a", executor: () => 0 },
  { id: "b", executor: () => 0 },
];
let observed = -1;

entries.some = (callback) => {
  observed = later;
};
const result = entries.some(() => false);

later = 42;
console.log(observed);
