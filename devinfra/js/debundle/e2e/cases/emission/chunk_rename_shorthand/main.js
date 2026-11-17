const { a } = { a: "A" };
function f(o) {
  return o.a;
}
console.log(f({ a }) + a);
export { a };
