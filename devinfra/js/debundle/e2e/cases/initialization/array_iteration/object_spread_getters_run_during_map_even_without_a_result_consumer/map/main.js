let later = 0;
let observed = -1;
const payload = {
  get value() {
    observed = later;
    return "ready";
  },
};
const entries = [{ payload }];
function copy(value) {
  return { ...value };
}
const result = entries.map((entry) => copy(entry.payload));
later = 42;
console.log(observed);
