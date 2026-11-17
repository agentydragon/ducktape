var dup = "outer";
function shadow() {
  var dup = "inner";
  return dup;
}
console.log(dup, shadow());
export { dup, shadow };
