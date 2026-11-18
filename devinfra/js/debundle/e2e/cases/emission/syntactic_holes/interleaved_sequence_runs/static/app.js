function runPipeline(flags) {
  return (flags.on, "marker", 1, 2, flags.off);
}
function otherPipeline(flags) {
  return (flags.on, 1, 2, flags.off);
}
console.log(runPipeline({ on: "left", off: "right" }));
console.log(otherPipeline({ on: "left", off: "other" }));
export { runPipeline };
