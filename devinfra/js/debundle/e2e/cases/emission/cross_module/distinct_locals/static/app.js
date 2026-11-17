function destructure({ readable: o }) {
  return o;
}
function alias() {
  const u = "y";
  return { readable: u };
}
function consumer({ readable: o }) {
  const u = String(o) + "x";
  return u;
}
console.log(destructure({ readable: 0 }), alias().readable, consumer({ readable: "v" }));
export { destructure, alias, consumer };
