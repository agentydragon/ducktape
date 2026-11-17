let state = "initial";
function setState(value) {
  state = value;
}
setState("updated");
console.log(state);
export { state, setState };
