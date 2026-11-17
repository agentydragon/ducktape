const a = new Proxy({ value: 1 }, {});
const b = new Proxy({ value: 2 }, {});
const c = new Proxy({ value: 3 }, {});
console.log(a.value, b.value, c.value);
export { a, b, c };
