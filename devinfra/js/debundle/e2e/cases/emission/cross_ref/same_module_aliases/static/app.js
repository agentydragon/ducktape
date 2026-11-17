class Acc1 {
  m1() {
    return "a1";
  }
}
class Acc2 {
  m2() {
    return "a2";
  }
}
const A2 = Acc1;
const B2 = Acc2;
console.log(new A2().m1() + new B2().m2());
export { Acc1, Acc2, A2, B2 };
