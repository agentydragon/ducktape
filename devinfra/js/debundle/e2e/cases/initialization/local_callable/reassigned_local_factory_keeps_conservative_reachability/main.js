function retain(read) {
  return { read };
}
retain = function replacement(read) {
  return { read };
};
const prefix = new globalThis.Date();
const holder = retain(() => later);
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return 1;
})({});
const later = dynamic;
