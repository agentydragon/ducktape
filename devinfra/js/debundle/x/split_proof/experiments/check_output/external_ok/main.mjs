import { area, registry } from "./base.mjs";
import { isEven, label } from "./text.mjs";
import "./plugins.mjs";

console.log(label("circle"), area(2), isEven(10));
console.log(registry.join(","));
