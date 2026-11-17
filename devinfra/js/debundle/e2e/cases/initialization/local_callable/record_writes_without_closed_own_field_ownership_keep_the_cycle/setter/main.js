function wrap(callback) {
  const result = {
    set run(value) {
      value();
    },
  };
  result.run = callback;
  return result;
}
const prefix = new globalThis.Date();
const holder = wrap(() => later);
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return 1;
})({});
const later = dynamic;
