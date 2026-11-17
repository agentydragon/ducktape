const { a } = { a: "A" };
function get() {
  return { a }.a + a;
}
console.log(get());
export { a, get };
