const receiver = {
  inspectReceiver() {
    consume(this);
  },
  readLater,
};
receiver.inspectReceiver();
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return 1;
})({});
const later = dynamic;
function readLater() {
  return later;
}
