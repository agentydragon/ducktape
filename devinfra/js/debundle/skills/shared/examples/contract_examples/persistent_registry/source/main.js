import { register, emit } from "./static/peer.js";
const registration = register(() => later);
const later = 42;
const observed = emit()[0];
console.log(observed);
