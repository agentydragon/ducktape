let later = 0;
const entries = [{ run: () => 0 }, {}];
const copies = entries.map((entry) => ({ ...entry }));
function consume(value = () => later) {
  return value();
}
const observed = consume(copies[1].run);
later = 42;
console.log(observed);
