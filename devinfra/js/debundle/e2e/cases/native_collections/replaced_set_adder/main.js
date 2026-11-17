Set.prototype.add = function (value) {
  console.log("add", value);
  throw new Error("stop");
};
try {
  new Set(["first", "second"]);
} catch (error) {
  console.log(error.message);
}
console.log("after");
