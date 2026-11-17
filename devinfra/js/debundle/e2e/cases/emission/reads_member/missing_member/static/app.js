const a = { present: 1 };
function readPresent() {
  return a.present;
}
console.log(readPresent());
export { a, readPresent };
