import { o as o$1 } from "./chunk-DuckMock.js";
const o = (t) => {
  const o2 = { text: o$1(t) };
  globalThis.__mockBundleState.chip = o2;
  document.querySelector("#chip").textContent = o2.text;
  return o2;
};
export {
  o as s
};
