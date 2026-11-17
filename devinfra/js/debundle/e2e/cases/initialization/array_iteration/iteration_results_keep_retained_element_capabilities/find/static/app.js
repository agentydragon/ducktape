import { consumeCallbacks } from "./peer.js";
let later = 0;
const entries = [
  { id: "a", executor: () => later },
  { id: "b", executor: () => later },
];
let observed = -1;

const result = entries.find((entry) => entry.id === "a");
observed = result.executor();
later = 42;
console.log(observed);
