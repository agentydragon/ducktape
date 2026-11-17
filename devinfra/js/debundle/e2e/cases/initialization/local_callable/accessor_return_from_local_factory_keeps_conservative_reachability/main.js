function retain(read) {
  return {
    get read() {
      return read();
    },
  };
}
const prefix = new globalThis.Date();
const holder = retain(() => later);
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return 1;
})({});
const later = dynamic;
