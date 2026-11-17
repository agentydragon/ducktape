const ctx = { id: 1 };
function readId() {
  return ctx.id;
}
console.log(readId());
export { ctx, readId };
