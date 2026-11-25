import { aa as R, bb as S } from "./vendor-abc.js";
const A = 4,
  B = R.map([1, 2, 3], (n) => n + A);
function f(n) {
  return g(n) + A;
}
function g(n) {
  return R.identity(n) * 2;
}
class K {
  constructor(n) {
    this.n = n;
  }
  get() {
    return f(this.n) + B.length;
  }
}
var h = (e) => new K(e).get();
const L = () => import("./chunk-x.js");
S.register("h", h);
console.log(h(3));
export { h, L };
