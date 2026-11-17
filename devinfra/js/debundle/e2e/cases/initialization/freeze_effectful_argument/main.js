function makeRecord(value) {
  return Object.freeze({ value });
}
const touch = () => later;
const early = makeRecord(touch());
var later = "later";
console.log(early.value, later);
export { early, later };
