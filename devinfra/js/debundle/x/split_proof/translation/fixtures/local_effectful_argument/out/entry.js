import "./side.js";
function identity(x) {
  return x;
}
function argument() {
  console.log("argument");
  return 1;
}
const result = identity(argument());
console.log("result", result);
