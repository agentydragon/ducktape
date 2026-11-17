// Re-exports and the call give entry direct reads of both cycle owners.
const dep_value = "alpha";
const cross_value = dep_value + "-beta";
function lazy_reader() {
  return cross_value;
}
console.log(dep_value, cross_value, lazy_reader());
export { dep_value, cross_value, lazy_reader };
