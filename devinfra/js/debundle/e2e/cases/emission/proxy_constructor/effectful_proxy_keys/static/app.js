const a = new Proxy({ nested: { [console.log("a")]() {} } }, {});
const b = new Proxy({ nested: { [console.log("b")]() {} } }, {});
const c = new Proxy({ nested: { [console.log("c")]() {} } }, {});
console.log("done");
export { a, b, c };
