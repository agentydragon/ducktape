class Accessor {
  tag() {
    return "acc";
  }
}
const alias = Accessor;
console.log(new alias().tag());
export { Accessor, alias };
