const X = "x-impl";
const av = "av-impl";
function usesAv() {
  return av;
}
console.log(X, av, usesAv());
export { X as av, usesAv };
