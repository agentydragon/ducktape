const prefix = new globalThis.Date();
function readLater() {
  return later;
}
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return 1;
})({});
const later = dynamic;
console.log(later);
