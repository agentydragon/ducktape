const source_value = "A";
function read(taken_name) {
  return taken_name;
}
console.log(source_value + read("B"));
export { source_value, read };
