function anchor(x) {
  return x + 1;
}
function delegate(x) {
  return anchor(x);
}
console.log(delegate(41));
export { anchor, delegate };
