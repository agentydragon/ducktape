const q = "a";
function r() {
  return q;
}
function s() {
  return "b" + r();
}
function t() {
  return s() + r();
}
console.log(t());
export { t, s };
