class Backend {
  constructor() {
    this.tag = "B";
  }
}
let currentLogger;
function setLogger(impl) {
  currentLogger = impl;
  globalThis.__tag = impl.tag;
}
setLogger(new Backend());
globalThis.__final = "done";
console.log(globalThis.__tag, globalThis.__final);
export { Backend, currentLogger, setLogger };
