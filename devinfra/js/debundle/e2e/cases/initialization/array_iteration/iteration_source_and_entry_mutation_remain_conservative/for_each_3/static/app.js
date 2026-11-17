import { consumeCallbacks } from "./peer.js";
let later = 0;
const entries = [
  { id: "a", executor: () => 0 },
  { id: "b", executor: () => 0 },
];
let observed = -1;

const result = entries.forEach(function (entry, index) {
  if (index === 0) arguments[2][1] = { executor: () => later };
  else observed = entry.executor();
});

later = 42;
console.log(observed);
