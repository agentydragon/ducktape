const a = 1;
const api = {
  run(f) {
    f();
    return Object;
  },
};
class C extends api.run(() => console.log(a)) {}
function back() {
  return C;
}
