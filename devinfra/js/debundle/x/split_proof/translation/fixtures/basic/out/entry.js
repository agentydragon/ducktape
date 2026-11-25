import Registry from "./vendors/registry.js";
import { Counter } from "./lib/counter.js";
var run = (event) => new Counter(event).get();
const loadExtra = () => import("../chunk-x.js");
Registry.hooks.register("h", run);
console.log(run(3));
export { run as h, loadExtra as L };
