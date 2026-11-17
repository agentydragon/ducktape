let T = "ready";
function readT() {
  return T;
}
const init = readT();
console.log(init);
export { T, readT, init };
