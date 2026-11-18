const runtimePrefix = "prefix",
  runtimeBuild = (value) => `build:${value}`,
  runtimeRead = (value) => runtimeBuild(value).toUpperCase(),
  runtimeSuffix = "suffix";
console.log(runtimePrefix, runtimeRead("one"), runtimeSuffix);
export { runtimePrefix, runtimeBuild, runtimeRead, runtimeSuffix };
