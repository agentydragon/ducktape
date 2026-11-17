import { prepare } from "./peer.js";
class Reader {
  read() {
    return later;
  }
}
const reader = new Reader();
class Cache {
  value = 0;
}
const marker = prepare();
const cache = new Cache();
const later = 42;
console.log(cache.value, reader.read(), later);
export { Reader, reader, Cache, cache, marker, later };
