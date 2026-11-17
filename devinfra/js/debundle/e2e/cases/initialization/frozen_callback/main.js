function makeRecord(callback) {
  return Object.freeze({ callback });
}
const early = makeRecord(() => later);
const later = "later";
console.log(early.callback());
export { early, later };
