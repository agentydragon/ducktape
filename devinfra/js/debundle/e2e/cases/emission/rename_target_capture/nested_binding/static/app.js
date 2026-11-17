var source_value = "A";
function f(taken_name) {
  return source_value + taken_name;
}
console.log(f("B"));
export { source_value, f };
