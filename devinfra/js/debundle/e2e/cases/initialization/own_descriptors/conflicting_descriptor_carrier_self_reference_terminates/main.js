function capture(record) {
  return Object.getOwnPropertyDescriptors(record);
}
const first = capture({});
const second = capture({ first });
Object.freeze(second);
console.log("done");
