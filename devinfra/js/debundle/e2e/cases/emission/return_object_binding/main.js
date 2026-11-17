var a = "foo";
function makeResult() {
  return { label: a };
}
console.log(makeResult().label);
export { makeResult };
