try {
  var impl = "primary";
} catch (e) {
  var impl = "fallback";
}
function reader() {
  return impl;
}
console.log(reader());
export { reader };
