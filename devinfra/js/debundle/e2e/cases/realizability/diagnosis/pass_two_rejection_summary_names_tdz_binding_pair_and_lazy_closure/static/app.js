const seed_value = "alpha";
const cross_value = seed_value + "-beta";
function lazy_back() {
  return cross_value;
}
export { seed_value, lazy_back };
