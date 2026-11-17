import { consumeCallbacks } from "./peer.js";
let later = 0;
const entries = [
  { id: "a", executor: () => 0 },
  { id: "b", executor: () => 0 },
];
let observed = -1;

const result = entries.some((entry, index, array) => {
  if (index === 0) array[1] = { executor: () => later };
  else observed = entry.executor();
  return false;
});

later = 42;
console.log(observed);
