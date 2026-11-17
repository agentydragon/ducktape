import "./peer.js";
new globalThis.Date();
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return 1;
})({});
const later = dynamic;
function readLater() {
  later;
}
export { dynamic, later, readLater };
