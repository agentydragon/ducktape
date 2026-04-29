// @ducktape-generated kind=lowerer-helper stage=selected_module_lowering ignore=detectors
// @ducktape-generator devinfra/js/debundle/extract/init_region.mjs
// Selected-module lowered region; original owners: owner_00003.

function oee(e, t) {
  for (var n = 0; n < t.length; n++) {
    const i = t[n];
    if (typeof i != "string" && !Array.isArray(i)) {
      for (const r in i) if (r !== "default" && !(r in e)) {
        const s = Object.getOwnPropertyDescriptor(i, r);
        s && Object.defineProperty(e, r, s.get ? s : {
          enumerable: !0,
          get: () => i[r]
        });
      }
    }
  }
  return Object.freeze(Object.defineProperty(e, Symbol.toStringTag, {
    value: "Module"
  }));
}
export { oee };
