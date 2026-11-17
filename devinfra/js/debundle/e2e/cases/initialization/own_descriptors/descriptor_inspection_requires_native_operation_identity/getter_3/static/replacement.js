export function replace() {
  Object.getOwnPropertyDescriptors = (record) => record.value;
}
