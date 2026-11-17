const init = ((globalThis.seq = "S"), "i");
export default class Boot {
  static {
    globalThis.seq = globalThis.seq + "C";
  }
}
const after = ((globalThis.seq = globalThis.seq + "Z"), "z");
console.log(globalThis.seq, init, after);
export { init, after };
