import Lodash from "../vendors/lodash.js";
import { limit } from "./consts.js";
function addBase(value) {
  return double(value) + limit;
}
function double(n) {
  return Lodash.identity(n) * 2;
}
export { addBase };
