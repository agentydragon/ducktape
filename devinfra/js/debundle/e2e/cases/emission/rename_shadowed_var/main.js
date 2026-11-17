var a = "A";
function f(b) {
  var a = "innerA";
  return a + b;
}
console.log(f("B") + a);
export { a, f };
