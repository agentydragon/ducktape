function z() {
  return "z";
}
var b = ({ p: c }) => c,
  f = ({ x: a = b }) => ({
    y: a,
    r: () =>
      a({
        p: "p",
      }),
  }),
  g = f({
    x: b,
  }),
  h = f({
    q: 1,
  });
console.log(g.r() + h.r() + z());
export { z, b, f };
