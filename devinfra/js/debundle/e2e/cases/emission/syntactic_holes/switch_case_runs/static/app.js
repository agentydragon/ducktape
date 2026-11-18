function dispatch(kind) {
  switch (kind) {
    case "alpha":
      return 1;
    case "beta":
      return 2;
    case "go":
      return 42;
    case "delta":
      return 4;
    default:
      return 0;
  }
}
console.log(dispatch("go"));
export { dispatch };
