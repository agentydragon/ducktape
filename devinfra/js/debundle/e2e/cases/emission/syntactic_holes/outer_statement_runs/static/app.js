const ignoredBefore = "before";
const runtimeFirst = "alpha";
function runtimeSecond() {
  return `${runtimeFirst}:beta`;
}
const ignoredAfter = "after";
console.log(runtimeSecond(), ignoredBefore, ignoredAfter);
export { ignoredBefore, runtimeFirst, runtimeSecond, ignoredAfter };
