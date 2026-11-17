import { h as x } from "./provider.js";
function call(readable) {
  return x() + readable;
}
console.log(call(3));
const guard = 0;
export { call, guard };
