const r = {
  register(x) {
    (this.i ||= []).push(x);
  },
};
class A {}
r.register(new A());
console.log("ok");
export { r, A };
