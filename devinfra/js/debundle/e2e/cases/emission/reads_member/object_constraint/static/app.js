const ctx = { id: "ctx-1" };
const node = { id: "node-1" };
function ctxId() {
  return ctx.id;
}
function nodeId() {
  return node.id;
}
console.log(ctxId(), nodeId());
export { ctx, node, ctxId, nodeId };
