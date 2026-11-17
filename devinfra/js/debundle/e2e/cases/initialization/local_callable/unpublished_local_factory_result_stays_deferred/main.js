function retain(read, write) {
  async function guarded() {
    return read();
  }
  return { read, write, guarded, preview: async () => read() };
}

const prefix = new globalThis.Date();
const holder = retain(
  () => later,
  () => 0
);
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return 1;
})({});
const later = dynamic;
console.log(later);
console.log(holder.read());
