import { e } from "./chunk-DuckMock.js";
const o = (t, a) => {
  const o2 = {
    badge: e(t, a),
    stamp: a.stamp,
    tags: t.tags.join(",")
  };
  globalThis.__mockBundleState.lazy = o2;
  document.querySelector("#status").textContent = o2.badge;
  return o2;
};
export {
  o as l
};
