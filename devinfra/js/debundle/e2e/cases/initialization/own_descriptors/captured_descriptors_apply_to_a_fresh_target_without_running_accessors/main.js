let getterCalls = 0;
let methodCalls = 0;
function wrap(record) {
  const descriptors = Object.getOwnPropertyDescriptors(record);
  const target = {};
  return Object.defineProperties(target, descriptors);
}
const record = {
  label: "copied",
  get value() {
    getterCalls++;
    return this.label + ":" + later;
  },
  make() {
    methodCalls++;
    return this.label + ":" + later;
  },
};
const result = wrap(record);
const beforeGetterCalls = getterCalls;
const beforeMethodCalls = methodCalls;
const later = "ready";
console.log(beforeGetterCalls, beforeMethodCalls, result.value, result.make(), getterCalls, methodCalls);
