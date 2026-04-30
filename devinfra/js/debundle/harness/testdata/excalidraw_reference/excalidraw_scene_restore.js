// @ducktape-generated kind=lowerer-helper stage=selected_module_lowering ignore=detectors
// @ducktape-generator devinfra/js/debundle/extract/init_region.mjs
// Selected-module lowered region; original owners: owner_00004, owner_00005, owner_00006.

function WO(type, t, props) {
  var key = null;
  if (props !== void 0 && (key = "" + props), t.key !== void 0 && (key = "" + t.key), "key" in t) {
    props = {};
    for (var r in t) r !== "key" && (props[r] = t[r]);
  } else props = t;
  return t = props.ref, {
    $$typeof,
    type,
    key,
    ref: t !== void 0 ? t : null,
    props
  };
}
const jO = {
    exports: {}
  },
  R5 = {};
let $$typeof, lee;
export function __dt_generated_init__atomic_module_0004__R5_WO_jO() {
  $$typeof = Symbol.for("react.transitional.element");
  lee = Symbol.for("react.fragment");
  R5.Fragment = lee;
  R5.jsx = WO;
  R5.jsxs = WO;
  jO.exports = R5;
}
export { jO };
