const a = { label: "first" }, b = (value) => value + "-second";
export const { label: c } = { label: "third" }, d = ["fourth"];
console.log(a.label, b("value"), c, d[0]);
