import { make } from "./static/peer.js";
const wrapped = make(() => later);
const later = 42;
console.log(wrapped.callback());
