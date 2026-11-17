var a = "A";
// The parameter collides with the outer binding's rename target, b.
function shadowVar(b) {
  var a = "innerA";
  return a + b;
}
function shadowParam(a) {
  return a;
}
function labelled() {
  a: for (;;) {
    break a;
  }
  return a;
}
console.log(shadowVar("B") + a);
console.log(shadowParam("arg") + a);
console.log(labelled());
export { a, shadowVar, shadowParam, labelled };
