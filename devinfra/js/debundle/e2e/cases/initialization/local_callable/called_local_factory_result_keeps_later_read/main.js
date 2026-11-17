function retain(read) {
  return { read };
}
const prefix = new globalThis.Date();
const holder = retain(() => later);
holder.read();
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return 1;
})({});
const later = dynamic;
