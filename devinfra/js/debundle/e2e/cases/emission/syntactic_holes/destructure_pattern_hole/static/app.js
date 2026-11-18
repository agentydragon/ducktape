function actual({ value, ignored }) {
  return `stable:${value}`;
}
console.log(actual({ value: "ok", ignored: "noise" }));
export { actual };
