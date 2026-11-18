const runtimePrefix = "prefix",
  runtimeFormat = (value) => String(value).toUpperCase(),
  runtimeLabels = new Map([
    ["left", "Left"],
    ["right", "Right"],
  ]),
  runtimeRead = (key) => runtimeLabels.get(key) ?? runtimeFormat(key),
  runtimeSuffix = "suffix";
console.log(runtimePrefix, runtimeRead("left"), runtimeSuffix);
export { runtimePrefix, runtimeFormat, runtimeLabels, runtimeRead, runtimeSuffix };
