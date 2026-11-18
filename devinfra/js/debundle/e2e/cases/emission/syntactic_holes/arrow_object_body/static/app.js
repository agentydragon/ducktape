const makeWidget = (props) => ({
  kind: "widget",
  render: () => props.label.toUpperCase(),
  dispose() {},
});
console.log(makeWidget({ label: "ok" }).render());
export { makeWidget };
