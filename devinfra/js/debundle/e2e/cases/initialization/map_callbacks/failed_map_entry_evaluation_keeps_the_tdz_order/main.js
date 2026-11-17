function opaqueConsumer(value) {
  globalThis.saved = value;
}
const result = "ok";
try {
  opaqueConsumer(new Map([[missing, () => later]]));
} catch {}
const missing = "key";
const later = 42;
console.log(result, globalThis.saved === undefined);
export { result, missing, later };
