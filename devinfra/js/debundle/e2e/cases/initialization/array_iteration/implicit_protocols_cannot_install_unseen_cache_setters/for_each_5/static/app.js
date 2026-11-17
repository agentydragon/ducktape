import { consumeCallbacks } from "./peer.js";
let later = 0;
const entries = [
  { id: "a", executor: () => 0 },
  { id: "b", executor: () => 0 },
];
let observed = -1;
const lookup = {};
function install() {
  Object.defineProperty(lookup, "a", {
    set(value) {
      observed = later;
    },
    configurable: true,
  });
}
const holder = {
  get field() {
    install();
    return 0;
  },
};
const { field } = holder;

const result = entries.forEach((entry) => {
  lookup[entry.id] = entry.id;
});

later = 42;
console.log(observed);
