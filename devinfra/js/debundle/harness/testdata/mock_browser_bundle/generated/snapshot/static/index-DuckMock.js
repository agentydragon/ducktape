const scriptRel = /* @__PURE__ */ (function detectScriptRel() {
  const relList = typeof document !== "undefined" && document.createElement("link").relList;
  return relList && relList.supports && relList.supports("modulepreload") ? "modulepreload" : "preload";
})();
const assetsURL = function(dep) {
  return "/" + dep;
};
const seen = {};
const __vitePreload = function preload(baseModule, deps, importerUrl) {
  let promise = Promise.resolve();
  if (deps && deps.length > 0) {
    let allSettled2 = function(promises) {
      return Promise.all(
        promises.map(
          (p) => Promise.resolve(p).then(
            (value) => ({ status: "fulfilled", value }),
            (reason) => ({ status: "rejected", reason })
          )
        )
      );
    };
    document.getElementsByTagName("link");
    const cspNonceMeta = document.querySelector(
      "meta[property=csp-nonce]"
    );
    const cspNonce = cspNonceMeta?.nonce || cspNonceMeta?.getAttribute("nonce");
    promise = allSettled2(
      deps.map((dep) => {
        dep = assetsURL(dep);
        if (dep in seen) return;
        seen[dep] = true;
        const isCss = dep.endsWith(".css");
        const cssSelector = isCss ? '[rel="stylesheet"]' : "";
        if (document.querySelector(`link[href="${dep}"]${cssSelector}`)) {
          return;
        }
        const link = document.createElement("link");
        link.rel = isCss ? "stylesheet" : scriptRel;
        if (!isCss) {
          link.as = "script";
        }
        link.crossOrigin = "";
        link.href = dep;
        if (cspNonce) {
          link.setAttribute("nonce", cspNonce);
        }
        document.head.appendChild(link);
        if (isCss) {
          return new Promise((res, rej) => {
            link.addEventListener("load", res);
            link.addEventListener(
              "error",
              () => rej(new Error(`Unable to preload CSS for ${dep}`))
            );
          });
        }
      })
    );
  }
  function handlePreloadError(err) {
    const e2 = new Event("vite:preloadError", {
      cancelable: true
    });
    e2.payload = err;
    window.dispatchEvent(e2);
    if (!e2.defaultPrevented) {
      throw err;
    }
  }
  return promise.then((res) => {
    for (const item of res || []) {
      if (item.status !== "rejected") continue;
      handlePreloadError(item.reason);
    }
    return baseModule().catch(handlePreloadError);
  });
};
const e$3 = "profile-7";
const t = "Ada Lovelace";
const a = ["analysis", "dom"];
const o$1 = () => ({ id: e$3, name: t, tags: [...a] });
const e$2 = (t2, a2) => t2 + a2;
const o = (t2) => t2.join("|");
const n = (t2) => Array.from(new Set(t2)).length;
const e$1 = { stamp: "mock-dashboard@7" };
const e = (a2) => e$2(a2.id.length, n(a2.tags));
class A {
  constructor(a2, t2) {
    this.profile = a2;
    this.total = t2;
  }
  snapshot() {
    return {
      profileName: this.profile.name,
      total: this.total,
      tags: this.profile.tags
    };
  }
}
const s = (a2) => new A(a2, e(a2));
const d = (a2) => ({
  headline: `${a2.profile.name}:${a2.total}`,
  total: a2.total,
  tags: o(a2.profile.tags),
  stamp: e$1.stamp
});
const h = (a2) => {
  const t2 = document.querySelector("#app");
  t2.dataset.bundle = "tana-like";
  t2.textContent = JSON.stringify(a2);
};
const g = async () => {
  const a2 = o$1();
  const t2 = s(a2);
  const o2 = d(t2);
  globalThis.__mockBundleState = { model: t2.snapshot(), summary: o2, lazy: null, chip: null };
  h(o2);
  const [{ l: e2 }, { s: i }] = await Promise.all([__vitePreload(() => import("./ActivityPanel-DuckMock.js"), true ? [] : void 0), __vitePreload(() => import("./SummaryChip-DuckMock.js"), true ? [] : void 0)]);
  e2(a2, o2);
  i(o2);
};
await g();
