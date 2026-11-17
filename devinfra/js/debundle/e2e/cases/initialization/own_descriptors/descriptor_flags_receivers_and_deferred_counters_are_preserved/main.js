let getterCalls = 0;
let methodCalls = 0;
const descriptors = Object.getOwnPropertyDescriptors({
  get value() {
    getterCalls++;
    return this.label + later;
  },
  method() {
    methodCalls++;
    return this.label + later;
  },
});
console.log(getterCalls, methodCalls);
const later = 42;
console.log(
  descriptors.value.get.call({ label: "getter:" }),
  descriptors.method.value.call({ label: "method:" }),
  getterCalls,
  methodCalls,
  descriptors.value.enumerable,
  descriptors.value.configurable,
  descriptors.method.writable
);
