import { h as x } from "./provider-a.js";
import { q as y } from "./provider-b.js";
console.log(x() + y());
const guard = 0;
export { x, y, guard };
