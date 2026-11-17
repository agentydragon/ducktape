import { f as cx } from "./vendor.js";
const a = (() => 1)();
const b = cx();
const c = a + b;
console.log(c);
export { a, b, c };
