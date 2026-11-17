const obj = { x: 1 };
function f() {
  with (obj) {
    return x;
  }
}
const A = 1;
export { A, f };
