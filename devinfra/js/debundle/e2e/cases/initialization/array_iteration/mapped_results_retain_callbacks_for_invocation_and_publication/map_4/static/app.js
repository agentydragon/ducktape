import { consumeCallbacks } from "./peer.js";
let later = 0;
const entries = [
  { id: "a", executor: () => 0 },
  { id: "b", executor: () => 0 },
];
let observed = -1;
function consume(array) {
  observed = array[0]();
}

const result = entries.map(() => () => later);
consume(result);
later = 42;
console.log(observed);
