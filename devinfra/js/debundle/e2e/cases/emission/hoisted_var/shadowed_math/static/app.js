try {
  var Math = { PI: "shimmed" };
} catch (e) {}
const out = Math.PI;
console.log(out);
export { out };
