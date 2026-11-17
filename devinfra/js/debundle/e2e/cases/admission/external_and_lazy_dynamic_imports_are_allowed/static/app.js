const lazyByName = (name) => import(name);
const loadVendor = () => import("some-external-package");
const A = "ok";
console.log(A);
export { A, lazyByName, loadVendor };
