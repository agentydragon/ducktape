let state = "initial";
function setupHandler() {
  globalThis.__readState = () => state;
}
setupHandler();
console.log(state);
export { state, setupHandler };
