async function k() {
  return "m";
}
class C {
  [await k()]() {}
}
console.log(C);
export { C };
