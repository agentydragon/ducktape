import { consumeCallbacks } from "./peer.js";
let later = 0;
const entries = [
  { id: "a", executor: () => 0 },
  { id: "b", executor: () => 0 },
];
let observed = -1;
function empty() {}
function consume(value = () => later) {
  return value();
}

const result = entries.map(() => {
  if (false) return 1;
});
observed = consume(result[0]);
later = 42;
console.log(observed);
