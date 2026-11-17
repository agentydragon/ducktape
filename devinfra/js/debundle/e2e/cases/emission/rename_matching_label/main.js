var a = "A";
function f() {
  a: for (;;) {
    break a;
  }
  return a;
}
console.log(f());
export { a, f };
