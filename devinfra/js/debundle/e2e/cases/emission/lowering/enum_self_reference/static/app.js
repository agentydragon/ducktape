var A = ((B) => {
  B.X = "x";
  B.Y = "y";
  return B;
})(A || {});
function b() {
  return A.X;
}
console.log(b());
export { b };
