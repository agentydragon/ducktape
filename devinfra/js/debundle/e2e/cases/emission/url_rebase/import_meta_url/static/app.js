function buildWorkerUrl() {
  return new URL("../worker-chunk/entry.js", import.meta.url);
}
console.log(typeof buildWorkerUrl);
export { buildWorkerUrl };
