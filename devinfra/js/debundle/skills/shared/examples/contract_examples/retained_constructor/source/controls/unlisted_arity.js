import { Box } from "./static/peer.js";
const wrapped = new Box(() => later, "metadata");
const later = 42;
console.log(wrapped.invoke());
