const slots = [];
function loadLabel(cache, label) {
  let memo;
  return (
    cache[0] !== label
      ? ((memo = async (id) => {
          const base = `load:${label}`;
          return `${base}:${id}`;
        }),
        (cache[0] = label),
        (cache[1] = memo))
      : (memo = cache[1]),
    memo
  );
}
// Same callback, but no assignment to memo: it must not match the selector.
function otherLabel(cache, label) {
  let memo;
  return (
    cache[0] !== label
      ? (async (id) => {
          const base = `load:${label}`;
          return `${base}:${id}`;
        },
        (cache[0] = label),
        (cache[1] = memo))
      : (memo = cache[1]),
    memo
  );
}
loadLabel(slots, "first")("a").then((value) => console.log(value));
export { loadLabel };
