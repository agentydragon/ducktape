import "./side.js";
function identity(x) {
  console.log("body");
  return x;
}
const result = identity(1);
console.log("result", result);
