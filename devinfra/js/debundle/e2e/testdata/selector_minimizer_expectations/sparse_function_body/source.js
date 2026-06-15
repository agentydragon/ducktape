function selectedWorker(x, y, z) {
  const transient = makeTransient(x, Date.now());
  const marker = 123;
  const generated = buildGenerated(transient);
  y.foo(z, 123);
  return generated;
}

function sameMarkerWorker(x, y, z) {
  const marker = 123;
  y.foo(z, 456);
  return x;
}

function sameCallWorker(x, y, z) {
  const marker = 456;
  y.foo(z, 123);
  return x;
}

function makeTransient(value) {
  return value;
}

function buildGenerated(value) {
  return value;
}

export { selectedWorker };
