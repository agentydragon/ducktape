import { consumeCallbacks } from "./peer.js";
let later = 0;
const entries = [
  { id: "a", executor: () => 0 },
  { id: "b", executor: () => 0 },
];
let observed = -1;
const holder = {
  items: entries,
  replace() {
    this.items[1] = { executor: () => later };
  },
};
holder.replace();
const result = entries.forEach((entry) => {
  observed = entry.executor();
});

later = 42;
console.log(observed);
