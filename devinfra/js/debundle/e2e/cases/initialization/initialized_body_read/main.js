const seed = 7;
const value = 42;
const result = readValue(seed);
function readValue(x) {
  return value + x;
}
console.log(result);
export { seed, result, readValue, value };
