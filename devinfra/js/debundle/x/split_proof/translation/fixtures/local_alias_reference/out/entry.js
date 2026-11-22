import "./side.js";
function identity(x) {
  return x;
}
const alias = identity;
const result = identity(1);
console.log("result", result);
