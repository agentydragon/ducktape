import { consumeCallbacks } from "./peer.js";
let later = 0;
const entries = [
  { id: "a", executor: () => 0 },
  { id: "b", executor: () => 0 },
];
let observed = -1;
class Publisher {
  install() {
    globalThis.slot = this;
  }
}
const publisher = new Publisher();
publisher.install.call(Array.prototype);
globalThis.slot.forEach = function (callback) {
  observed = later;
};
const result = entries.forEach(() => {});

later = 42;
console.log(observed);
