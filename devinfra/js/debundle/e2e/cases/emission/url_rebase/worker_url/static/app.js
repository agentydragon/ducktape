function a() {
  return new Worker("./b.js");
}
console.log(typeof a);
export { a };
