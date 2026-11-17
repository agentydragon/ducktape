import { consumeCallbacks } from "./peer.js";
let later = 0;
const entries = [
  { id: "a", executor: () => later },
  { id: "b", executor: () => later },
];
let observed = -1;

const result = entries.forEach((entry) => {
  observed = entry.executor();
});

later = 42;
console.log(observed);
