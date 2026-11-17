let state = "initial";
function setupHandler() {
  globalThis.__updateState = () => {
    state = "updated";
  };
}
setupHandler();
console.log(state);
export { state, setupHandler };
