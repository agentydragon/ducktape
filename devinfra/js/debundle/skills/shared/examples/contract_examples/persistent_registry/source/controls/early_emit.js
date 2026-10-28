import { register, emit } from "./static/peer.js";
const registration = register(() => later);
const observed = emit()[0];
const later = 42;
console.log(observed);
