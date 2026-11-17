console.log("before");
var seed = new Set(["a", "a", "b"]),
  sibling = new Set(["x", "x"]),
  copied = new Set([...seed, "b", "c", "c"]);
console.log("after", copied.size, [...copied].join(","));
