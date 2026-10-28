import { make } from "./static/peer.js";
const callback = () => later;
const wrapped = make(callback);
const later = 42;
console.log(wrapped.callback());
