const q = () => "a";
const b = ({ a: c = q } = {}) => c();
console.log(b({}), b({ a: () => "b" }));
export { q, b };
