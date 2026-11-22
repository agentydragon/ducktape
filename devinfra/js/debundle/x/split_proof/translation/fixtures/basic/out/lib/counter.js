import { items } from "./consts.js";
import { addBase } from "./math.js";
class Counter {
  constructor(start) {
    this.n = start;
  }
  get() {
    return addBase(this.n) + items.length;
  }
}
export { Counter };
