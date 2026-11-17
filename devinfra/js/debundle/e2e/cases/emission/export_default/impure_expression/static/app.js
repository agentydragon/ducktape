const init = ((globalThis.seq = "S"), "i");
export default (globalThis.seq = globalThis.seq + "D");
const after = ((globalThis.seq = globalThis.seq + "Z"), "z");
console.log(globalThis.seq, init, after);
export { init, after };
