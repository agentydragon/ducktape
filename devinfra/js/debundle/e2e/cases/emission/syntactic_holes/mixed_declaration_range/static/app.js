const runtimeFirstClassName = "Widget-module_first__mix1";
const runtimeSecondClassName = "Widget-module_second__mix2";
function runtimeMakeStyles(first, second) {
  return { first, second };
}
const runtimeStyles = runtimeMakeStyles(runtimeFirstClassName, runtimeSecondClassName);
console.log(runtimeStyles.first, runtimeStyles.second);
export { runtimeFirstClassName, runtimeSecondClassName, runtimeMakeStyles, runtimeStyles };
