try {
  var impl = "primary";
} catch (e) {
  var impl = "fallback";
}
const reader = impl + "-read";
console.log(reader);
export { reader };
