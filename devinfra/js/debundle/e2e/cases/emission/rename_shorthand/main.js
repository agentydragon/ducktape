const { a } = { a: "A" };
function get(o) {
  return o.a + { a }.a + a;
}
console.log(get({ a }));
export { a, get };
