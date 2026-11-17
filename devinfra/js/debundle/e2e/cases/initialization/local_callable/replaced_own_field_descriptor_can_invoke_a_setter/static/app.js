import { prepare } from "./peer.js";
let later = 0;
let observed = -1;
class Cache {
  value = 0;
}
const marker = prepare();
const cache = new Cache();
Object.defineProperty(cache, "value", {
  set(value) {
    observed = later;
  },
  configurable: true,
});
cache.value = 1;
later = 42;
console.log(observed, later);
export { Cache, cache, marker, later, observed };
