const a = { nested: { [console.log("a")]() {} } };
const b = { nested: { [console.log("b")]() {} } };
const c = { nested: { [console.log("c")]() {} } };
console.log("done");
export { a, b, c };
