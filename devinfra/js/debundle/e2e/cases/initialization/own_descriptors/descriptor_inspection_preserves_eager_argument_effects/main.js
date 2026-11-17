const effects = [];
function makeValue() {
  effects.push("argument");
  return 1;
}
const descriptors = Object.getOwnPropertyDescriptors({
  field: makeValue(),
  get value() {
    effects.push("getter");
    return later;
  },
});
const later = 42;
console.log(effects.join(","), descriptors.field.value, descriptors.value.get(), effects.join(","));
