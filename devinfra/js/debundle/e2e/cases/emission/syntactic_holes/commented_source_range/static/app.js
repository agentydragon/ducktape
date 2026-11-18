const runtimeFirstClassName = "Widget-module_first__c0m";
// Deliberately between declarations; comments should not become selector anchors.
const runtimeSecondClassName = "Widget-module_second__m3nt";
/* Another ignored comment before the aggregate object. */
const runtimeStyles = {
  first: runtimeFirstClassName,
  second: runtimeSecondClassName,
};
console.log(runtimeStyles.first, runtimeStyles.second);
export { runtimeFirstClassName, runtimeSecondClassName, runtimeStyles };
