function a(o) {
  const n = o.value;
  return { value: n };
}
console.log(JSON.stringify(a({ value: 1 })));
export { a };
