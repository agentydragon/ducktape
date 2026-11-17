function reader() {
  return (0, eval)("1 + 1") + Function("return 1")() + new Function("return 1")();
}
const A = "ok";
console.log(A);
export { A, reader };
