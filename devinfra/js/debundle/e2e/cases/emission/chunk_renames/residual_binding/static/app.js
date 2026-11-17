console.log("before");
let x = ((globalThis.__touched = true), "x-value");
console.log(x);
export { x };
