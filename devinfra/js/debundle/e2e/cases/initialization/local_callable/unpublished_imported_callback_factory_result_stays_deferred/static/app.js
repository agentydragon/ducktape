import { observe } from "./peer.js";
const prefix = new globalThis.Date();
const wrapped = observe(() => later);
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return 1;
})({});
const later = dynamic;
console.log(later);
console.log(wrapped());
