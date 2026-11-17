const r = {
  register(x) {
    (this.i ||= []).push(x);
  },
};
class A {}
class B {}
r.register(A);
r.register(B);
console.log(new A().constructor.name, new B().constructor.name);
export { r, A, B };
