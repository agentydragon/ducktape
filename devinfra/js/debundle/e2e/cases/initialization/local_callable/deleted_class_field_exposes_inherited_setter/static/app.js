import { prepare } from "./peer.js";
let later = 0;
let observed = -1;
class Cache {
  value = 0;
}
Object.defineProperty(Cache.prototype, "value", {
  set(value) {
    observed = later;
  },
  configurable: true,
});
const marker = prepare();
const cache = new Cache();
delete cache.value;
cache.value = 1;
later = 42;
console.log(observed, later);
export { Cache, cache, marker, later, observed };
