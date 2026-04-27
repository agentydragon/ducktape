import { e as r } from "./profile.js";
import { a as t, c as o, d as l } from "./math.js";
import { i as n } from "./runtimeInfo.js";

const e = (a) => t(a.id.length, l(a.tags));
class A {
  constructor(a, t2) {
    this.profile = a;
    this.total = t2;
  }

  snapshot() {
    return {
      profileName: this.profile.name,
      total: this.total,
      tags: this.profile.tags,
    };
  }
}

const s = (a) => new A(a, e(a));
const d = (a) => ({
  headline: `${a.profile.name}:${a.total}`,
  total: a.total,
  tags: o(a.profile.tags),
  stamp: n.stamp,
});
const h = (a) => {
  const t2 = document.querySelector("#app");
  t2.dataset.bundle = "mock-app";
  t2.textContent = JSON.stringify(a);
};

const g = async () => {
  const a = r();
  const t2 = s(a);
  const o2 = d(t2);
  globalThis.__mockBundleState = { model: t2.snapshot(), summary: o2, lazy: null, chip: null };
  h(o2);
  const [{ l: e2 }, { s: i }] = await Promise.all([import("./ActivityPanel.js"), import("./SummaryChip.js")]);
  e2(a, o2);
  i(o2);
};

await g();
export { A as AppModel, g as start };
