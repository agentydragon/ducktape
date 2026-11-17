import { sA, sB } from "./provider_module.js";
function makeConfig() {
  return { propKeyA: sA, propKeyB: sB };
}
console.log(makeConfig().propKeyA + ":" + makeConfig().propKeyB);
export { makeConfig };
