import Lodash from "../vendors/lodash.js";
const limit = 4,
  items = Lodash.map([1, 2, 3], (n) => n + limit);
export { items, limit };
