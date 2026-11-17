function makeRecord(name, header) {
  return Object.freeze({ name, header });
}
const peek = () => later;
globalThis.peek = peek;
const early = makeRecord("first", "ready");
const later = "later";
console.log(early.name, globalThis.peek());
export { early, later };
