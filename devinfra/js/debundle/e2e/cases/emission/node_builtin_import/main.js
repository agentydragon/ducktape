const live = 1;
import { basename as dep } from "node:path";
console.log(live, dep("/tmp/example"));
