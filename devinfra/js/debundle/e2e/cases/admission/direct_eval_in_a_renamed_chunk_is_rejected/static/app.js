function reader() {
  return eval("A + 1");
}
const A = 1;
console.log(reader());
export { A, reader };
