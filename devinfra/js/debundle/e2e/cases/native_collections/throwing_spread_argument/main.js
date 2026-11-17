const seed = new Set(["a"]);
try {
  new Set([
    ...seed,
    (() => {
      console.log("tail");
      throw new Error("stop");
    })(),
    console.log("never"),
  ]);
} catch (error) {
  console.log(error.message);
}
console.log("after");
