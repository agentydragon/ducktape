import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";

const require = createRequire(pathToFileURL(process.env.AW_WEBUI_PACKAGE_JSON));
const VueModule = require("vue");
const Vue = VueModule.default || VueModule;
const { PiniaVuePlugin, createPinia, defineStore, setActivePinia } = require("pinia");
const { isVue2, isVue3 } = require("vue-demi");

Vue.use(PiniaVuePlugin);
setActivePinia(createPinia());

const useStore = defineStore("vue-demi-vue2-integration", {
  state: () => ({
    buckets: [],
    info: null,
  }),
});
const store = useStore();

store.info = { version: "v0.0.0 (rust)" };
store.buckets = [{ id: "aw-watcher-window_test" }];

if (!isVue2 || isVue3) {
  throw new Error(`vue-demi selected the wrong runtime: isVue2=${isVue2}, isVue3=${isVue3}`);
}
if (store.info.version !== "v0.0.0 (rust)") {
  throw new Error(`Pinia did not unwrap object state: ${JSON.stringify(store.info)}`);
}
if (!Array.isArray(store.buckets) || store.buckets[0].id !== "aw-watcher-window_test") {
  throw new Error(`Pinia did not unwrap array state: ${JSON.stringify(store.buckets)}`);
}
