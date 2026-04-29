// @ducktape-generated kind=lowerer-helper stage=selected_module_lowering ignore=detectors
// @ducktape-generator devinfra/js/debundle/extract/init_region.mjs
// Selected-module lowered region; original owners: owner_00001.

import { iee } from "./atomic_module_0000__iee.js";
let ree;
export function __dt_generated_init__atomic_module_0001__ree() {
  ree = (e, t, value) => t in e ? iee(e, t, {
    enumerable: !0,
    configurable: !0,
    writable: !0,
    value
  }) : e[t] = value;
}
export { ree };
