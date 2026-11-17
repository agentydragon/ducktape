import * as gen from "./gen.js";
function nextId() {
  return gen.next();
}
console.log(nextId());
export { nextId };
