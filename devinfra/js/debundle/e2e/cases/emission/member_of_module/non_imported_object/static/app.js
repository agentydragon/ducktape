const gen = {
  next() {
    return 3;
  },
};
function a() {
  return gen.next();
}
console.log(a());
export { gen, a };
