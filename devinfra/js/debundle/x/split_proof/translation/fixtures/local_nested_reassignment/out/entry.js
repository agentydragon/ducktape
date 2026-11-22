import "./side.js";
function identity(x) {
  return x;
}
function replace() {
  identity = function (x) {
    console.log("nested");
    return x;
  };
}
replace();
const result = identity(1);
console.log("result", result);
