function joinParts(...parts) {
  return parts.join("|");
}
function ignoredPart(label) {
  return `ignored:${label}`;
}
const importantValue = "important";
const actual = joinParts("stable", ignoredPart("left"), importantValue, ignoredPart("right"));
console.log(actual);
export { actual };
