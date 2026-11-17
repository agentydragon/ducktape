function wrap(callback, tag) {
  const result = { run: null, tag: 0 };
  result.run = callback;
  result.tag = tag;
  return result;
}
const prefix = new globalThis.Date();
const holder = wrap(() => later, 2);
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return 1;
})({});
const later = dynamic;
console.log(later, holder.run(), holder.tag);
