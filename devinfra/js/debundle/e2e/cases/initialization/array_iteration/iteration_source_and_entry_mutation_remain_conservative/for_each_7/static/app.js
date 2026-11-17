import { consumeCallbacks } from "./peer.js";
let later = 0;
const entries = [
  { id: "a", executor: () => 0 },
  { id: "b", executor: () => 0 },
];
let observed = -1;
function replace() {
  arguments[0][1] = { executor: () => later };
}
replace(entries);
const result = entries.forEach((entry) => {
  observed = entry.executor();
});

later = 42;
console.log(observed);
