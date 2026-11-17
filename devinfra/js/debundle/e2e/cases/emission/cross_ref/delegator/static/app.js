function EBt(x) {
  return x + 1;
}
function UBt(x) {
  return EBt(x);
}
console.log(UBt(41));
export { EBt, UBt };
