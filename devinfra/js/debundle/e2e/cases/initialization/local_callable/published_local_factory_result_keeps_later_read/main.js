function retain(read) {
  return { read };
}
const prefix = new globalThis.Date();
const holder = retain(() => later);
globalThis.saved = holder;
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return 1;
})({});
const later = dynamic;
