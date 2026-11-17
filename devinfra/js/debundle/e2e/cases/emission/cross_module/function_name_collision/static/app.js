function a() {
  return "provider";
}
function b() {
  return "local";
}
function c() {
  return a() + ":" + b();
}
console.log(c());
export { a, b, c };
