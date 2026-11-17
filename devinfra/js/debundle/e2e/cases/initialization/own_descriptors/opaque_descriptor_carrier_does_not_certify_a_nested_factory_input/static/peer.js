export function factory(record) {
  const eager = record.nested.value.value;
  return () => eager;
}
