var value = false;
var alternate = false;
var writes = 0;
function set(next) {
  value = next;
  writes++;
}
export { value, alternate, set, writes };
