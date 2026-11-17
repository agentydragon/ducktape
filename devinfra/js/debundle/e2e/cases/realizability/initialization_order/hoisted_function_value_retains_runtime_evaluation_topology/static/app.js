function factory() {
  return 1;
}
const command = { run: factory };
const list = [command.run()];
function readList() {
  return list;
}
console.log(list[0]);
export { factory, command, list, readList };
