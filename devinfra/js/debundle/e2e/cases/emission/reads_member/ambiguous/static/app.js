const a = { shared: 1 };
function readA() {
  return a.shared;
}
function readToo() {
  return a.shared;
}
console.log(readA(), readToo());
export { a, readA, readToo };
