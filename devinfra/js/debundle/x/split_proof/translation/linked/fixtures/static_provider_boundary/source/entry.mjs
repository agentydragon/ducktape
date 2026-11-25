import { value as live, set } from "./provider.mjs";
set(true);
var snapshot = live;
export { snapshot, live };
