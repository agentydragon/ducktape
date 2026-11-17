const gen = {
  counter: 0,
  nextUniqueId() {
    return ++this.counter;
  },
};
function readId() {
  return gen.nextUniqueId();
}
console.log(readId());
export { gen, readId };
