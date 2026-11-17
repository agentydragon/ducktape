import { h } from "./provider.js";
import * as ns from "./provider.js";
console.log(h(), ns.h());
import("./provider.js").then(dynamic => console.log(dynamic.h()));
