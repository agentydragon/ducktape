let t = "T";
function f({ readable: t }) {
  return t;
}
console.log(f({ readable: "R" }) + t);
export { t, f };
