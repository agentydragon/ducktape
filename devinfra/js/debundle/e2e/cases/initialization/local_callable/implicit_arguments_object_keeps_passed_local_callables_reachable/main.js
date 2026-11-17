function exposeArgument(callback) {
  consume(arguments);
}
exposeArgument(readLater);
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return 1;
})({});
const later = dynamic;
function readLater() {
  return later;
}
