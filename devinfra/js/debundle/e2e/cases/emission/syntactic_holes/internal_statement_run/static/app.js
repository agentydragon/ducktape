const runtimeFirst = "alpha";
const ignoredMiddle = "middle";
function runtimeSecond() {
  return `${runtimeFirst}:beta`;
}
console.log(runtimeSecond(), ignoredMiddle);
export { runtimeFirst, ignoredMiddle, runtimeSecond };
