async function fetchData() {
  return await Promise.resolve(42);
}
const promise = fetchData();
promise.then((v) => console.log(v));
export { fetchData, promise };
