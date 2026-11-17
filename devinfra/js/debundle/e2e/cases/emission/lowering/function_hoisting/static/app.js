function a() {
  return b();
}
const c = a();
function b() {
  return "b";
}
console.log(c);
export { c };
