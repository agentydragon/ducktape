import "./side.js";
function identity(x) {
  return x;
}
identity = function (x) {
  console.log("replacement");
  return x;
};
const result = identity(1);
console.log("result", result);
