var a = "foo";
function factory() {
  return { label: a };
}
function direct() {
  return a;
}
console.log(factory().label + ":" + direct());
export { factory, direct };
