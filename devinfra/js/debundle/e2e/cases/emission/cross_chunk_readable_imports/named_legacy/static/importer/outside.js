import { h } from "../provider/entry.js";
import * as ns from "../provider/entry.js";
console.log(h(), ns.h());
import("../provider/entry.js").then((dynamic) => console.log(dynamic.h()));
