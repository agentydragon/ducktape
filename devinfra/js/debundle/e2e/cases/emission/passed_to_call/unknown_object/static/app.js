const r = {
  register(x) {
    (this.i ||= []).push(x);
  },
};
class A {}
r.register(A);
console.log(new A().constructor.name);
export { r, A };
