import * as gen from "./gen.js";
function a() {
  return gen.next();
}
function b() {
  return gen.next();
}
console.log(a(), b());
export { a, b };
