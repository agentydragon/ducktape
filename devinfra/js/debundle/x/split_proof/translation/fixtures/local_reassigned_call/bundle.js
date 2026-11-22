function identity(x) {
  return x;
}
identity = function (x) {
  console.log("replacement");
  return x;
};
const result = identity(1);
console.log("side");
console.log("result", result);
