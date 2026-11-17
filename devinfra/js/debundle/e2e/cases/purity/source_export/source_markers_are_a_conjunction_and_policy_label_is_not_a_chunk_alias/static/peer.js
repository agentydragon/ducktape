import { createRequire } from "node:module";
export const purpose = "neutral-lazy-registry";
export const version = "private-callback-storage";
const nativeRequire = import.meta.require ?? createRequire(import.meta.url);
export const resourceUrl = new URL("./resource.cjs", import.meta.url);
export function readResource() {
  return nativeRequire("./resource.cjs");
}
var factory = (callback) => {
  const wrapped = () => callback();
  return wrapped;
};
export { factory };
