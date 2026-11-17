import { consumeCallbacks } from "./peer.js";
let later = 0;
const entries = [
  { id: "a", executor: () => 0 },
  { id: "b", executor: () => 0 },
];
let observed = -1;
function replace({ items }) {
  items[1] = { executor: () => later };
}
replace({ items: entries });
const result = entries.forEach((entry) => {
  observed = entry.executor();
});

later = 42;
console.log(observed);
