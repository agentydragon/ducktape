var state = { value: "before" };
function readValue() {
  return state.value;
}
const before = readValue();
if (true) {
  var state = {
    get value() {
      console.log("getter");
      return "after";
    },
  };
}
const after = readValue();
console.log(before, after);
export { before, after };
