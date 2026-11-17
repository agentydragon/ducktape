let later = 0;
class Reader {
  static async read() {
    await 0;
    console.log(later);
  }
}
const holder = {
  get value() {
    Reader.read();
    return 0;
  },
};
const observed = holder.value;
later = 42;
console.log(observed);
