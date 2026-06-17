function firstHelper() {
  return wrap();
}
function selectedHelper() {
  return wrap();
}
function neighborWithToken(input) {
  const prepared = normalize(input);
  emit("neighbor-unique-token");
  cleanup(prepared);
}
export { selectedHelper };
