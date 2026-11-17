import { consumeCallbacks } from "./peer.js";
let later = 0;
const entries = [
  { id: "a", executor: () => 0 },
  { id: "b", executor: () => 0 },
];
let observed = -1;
function mutate() {
  arguments[0][1] = { executor: () => later };
}

const result = entries.forEach((entry, index, array) => {
  if (index === 0) mutate(array);
  else observed = entry.executor();
});

later = 42;
console.log(observed);
