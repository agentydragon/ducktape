import { e, t, r } from "./provider.js";
function a({ value: e }) {
  return e + 1;
}
function b(e) {
  const n = r(e);
  return { item: n };
}
class C {
  constructor(e) {
    this.registry = e;
  }
}
function d() {
  return { options: t };
}
function s(t) {
  return t * 2;
}
function g({ payload: e }) {
  return e.length;
}
console.log(a({ value: 1 }), b(2).item.size, new C(3).registry, d().options.kind, s(4), g({ payload: "xy" }));
export { a, b, C, d, s, g };
