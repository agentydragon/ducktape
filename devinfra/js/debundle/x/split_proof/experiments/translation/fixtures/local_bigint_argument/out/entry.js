import "./side.js";
function identity(x) {
  return x;
}
const result = identity(1n);
console.log("result", result);
