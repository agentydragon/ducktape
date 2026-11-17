console.log("before");
const a = console.log("middle");
function provider() {
  return "ok";
}
console.log("after", a);
export { a, provider };
