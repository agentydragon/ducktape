import { consumeCallbacks } from "./peer.js";
let later = 0;
const entries = [
  { id: "a", executor: () => 0 },
  { id: "b", executor: () => 0 },
];
let observed = -1;
class Matcher {}
const value = new Proxy(
  {},
  {
    getPrototypeOf() {
      observed = later;
      return null;
    },
  }
);

const result = value instanceof Matcher;

later = 42;
console.log(observed);
