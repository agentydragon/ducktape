import { retain } from "./peer.js";
let later = 0;
function createReader() {
  return () => later;
}
const holder = { value: 1 };
retain(createReader);
retain(holder);
const observed = holder.missing;
later = 42;
console.log(observed);
