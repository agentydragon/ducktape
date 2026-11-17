class Acc {
  tag() {
    return "acc";
  }
}
const HI = Acc;
console.log(new HI().tag());
export { Acc, HI };
