var a = null;
Promise.resolve().then(() => {
  a = true;
});
console.log(a);
export { a };
