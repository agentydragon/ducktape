const gen = {
  counter: 0,
  nextUniqueId() {
    return ++this.counter;
  },
};
function ls() {
  return gen.nextUniqueId();
}
console.log(ls());
export { gen, ls };
