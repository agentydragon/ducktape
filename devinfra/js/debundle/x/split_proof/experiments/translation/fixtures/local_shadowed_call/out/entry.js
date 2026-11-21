import "./side.js";
function identity(x) {
  return x;
}
function shadow(identity) {
  return identity;
}
const result = identity(1);
console.log("result", result);
