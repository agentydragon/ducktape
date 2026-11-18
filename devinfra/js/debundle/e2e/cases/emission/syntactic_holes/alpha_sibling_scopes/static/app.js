function actual(items) {
  return items
    .map((x) => {
      const y = [];
      y.push(x.label);
      return y.join(":");
    })
    .filter((x) => x !== "")
    .map((x) => x.toUpperCase())
    .join(",");
}
console.log(actual([{ label: "left" }, { label: "right" }]));
export { actual };
