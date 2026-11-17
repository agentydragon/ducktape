const a = (x) => "h:" + x;
const b = (x) => a(x);
console.log(b("y"));
export { b };
