import { Box } from "./static/peer.js";
const wrapped = new Box(() => later);
const later = 42;
console.log(wrapped.invoke());
