const holder = {};
function consume(record) {
  return record.holder.value.read();
}
const descriptors = Object.getOwnPropertyDescriptors({ holder });
holder.read = () => later;
const before = consume(descriptors);
const later = 42;
