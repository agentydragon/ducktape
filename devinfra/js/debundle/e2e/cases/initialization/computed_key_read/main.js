const x1 = { id: "x1" };
const y = { id: "k" };
const x2 = { [y.id]: "v" };
console.log(x1.id, y.id, x2[y.id]);
export { x1, y, x2 };
