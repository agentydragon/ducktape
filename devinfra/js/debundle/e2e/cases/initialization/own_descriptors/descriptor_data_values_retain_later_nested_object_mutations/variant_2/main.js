const holder = {};
function consume(record) {
  return record.holder.value.read();
}
const descriptors = Object.getOwnPropertyDescriptors({ holder });
holder.read = () => later;
let observed;
try {
  consume(descriptors);
} catch (error) {
  observed = error.name;
}
const later = 42;
console.log(observed, later);
