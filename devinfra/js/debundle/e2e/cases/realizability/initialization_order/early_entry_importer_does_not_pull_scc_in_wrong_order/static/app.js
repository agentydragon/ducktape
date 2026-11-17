const T = "ready";
function readT() {
  return T;
}
function disableDevMode() {
  return T;
}
const init = readT();
function middleHelper() {
  return init + "-mid";
}
function loggerReader() {
  return middleHelper();
}
console.log(init);
export { T, readT, init, disableDevMode, middleHelper, loggerReader };
