function makeValue(label) {
  return label.toUpperCase();
}
const actual = {
  requiredKey: makeValue("required"),
  generatedAlpha: makeValue("alpha"),
  nested: { inner: makeValue("nested") },
  ...{ spreadValue: makeValue("spread") },
  anotherKey: makeValue("another"),
};
console.log(actual.requiredKey, actual.anotherKey, actual.spreadValue);
export { actual };
