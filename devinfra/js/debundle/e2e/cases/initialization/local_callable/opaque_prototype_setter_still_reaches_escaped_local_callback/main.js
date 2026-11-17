const earlier = "later";
let observed = "";
function readLater() {
  return earlier + String(later);
}
Object.defineProperty(Object.prototype, "__debundle_test_probe__", {
  set(value) {
    observed = readLater();
  },
});
const dynamic = ((object) => {
  object[(object.key = "__debundle_test_probe__")] = "value";
  return observed;
})({});
var later = dynamic;
console.log(dynamic);
