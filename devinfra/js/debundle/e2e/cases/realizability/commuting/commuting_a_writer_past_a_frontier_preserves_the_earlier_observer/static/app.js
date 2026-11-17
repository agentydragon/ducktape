const store = [];
function read() {
  return store.length;
}
function write() {
  store.push(1);
}
function noop() {}
const anchor = 0;
const gamma = read();
const beta = noop?.();
const alpha = write();
console.log(gamma, beta, alpha, store.length);
