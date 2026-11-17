var a = "foo",
  label = "bar";
function makeResult() {
  return { label: a, other: label.toUpperCase() };
}
console.log(makeResult().label + ":" + makeResult().other);
export { makeResult };
