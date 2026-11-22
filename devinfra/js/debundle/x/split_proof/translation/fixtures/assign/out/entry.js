import { count, inc, read } from "./counter.js";
inc();
const seen = count;
console.log(seen, read());
