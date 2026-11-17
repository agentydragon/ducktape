import { x as importedThing, y } from "../vendor/entry.js";
let x = "local";
console.log(x, importedThing, y);
export { x };
