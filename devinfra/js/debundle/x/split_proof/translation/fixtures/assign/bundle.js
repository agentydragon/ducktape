let count = 0;
function inc() {
  count++;
}
function read() {
  return count;
}
inc();
const seen = count;
console.log(seen, read());
