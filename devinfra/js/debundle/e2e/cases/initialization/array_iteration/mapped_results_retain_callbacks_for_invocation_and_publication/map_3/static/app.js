import { consumeCallbacks } from "./peer.js";
let later = 0;
const entries = [
  { id: "a", executor: () => 0 },
  { id: "b", executor: () => 0 },
];
let observed = -1;

const result = entries.map(() => ({
  get value() {
    return later;
  },
}));
observed = result[0].value;
later = 42;
console.log(observed);
