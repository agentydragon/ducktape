function selectedCall(foo, input) {
  const prepared = prepareValue(input);
  const result = foo.bar(prepared, 123);
  cleanupValue(prepared);
  return result;
}

function sameMethodDifferentLiteral(foo, input) {
  const prepared = prepareValue(input);
  return foo.bar(prepared, 456);
}

function sameLiteralDifferentMethod(foo, input) {
  const prepared = prepareValue(input);
  return foo.baz(prepared, 123);
}

function prepareValue(value) {
  return value;
}

function cleanupValue(value) {
  return value;
}

export { selectedCall };
