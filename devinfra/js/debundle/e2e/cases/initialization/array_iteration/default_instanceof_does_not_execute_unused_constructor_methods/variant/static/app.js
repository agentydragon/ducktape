import { getValue } from "./peer.js";
let later = 0;
class Matcher {
  unused() {
    return later;
  }
}
const matches = {} instanceof Matcher;
later = 42;
console.log(matches);
