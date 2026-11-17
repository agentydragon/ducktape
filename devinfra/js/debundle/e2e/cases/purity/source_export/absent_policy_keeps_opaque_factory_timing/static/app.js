import { factory } from "./peer.js";
const wrapped = factory(() => later);
const later = 42;
console.log(wrapped());
