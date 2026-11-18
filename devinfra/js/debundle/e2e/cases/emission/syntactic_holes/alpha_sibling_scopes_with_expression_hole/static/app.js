function actual(items) {
  return items
    .map((x) => x.label.trim())
    .filter((x) => x !== "")
    .map((x) => x.toUpperCase())
    .join(",");
}
console.log(actual([{ label: " left " }, { label: " right " }]));
export { actual };
