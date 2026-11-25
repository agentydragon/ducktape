// Node/V8 observation only. No concrete stack string is compared or installed in CoreJS.
import assert from "node:assert/strict";

const Alias = Error;
const error = new Alias();
assert.equal(Object.getPrototypeOf(error), Error.prototype);
assert.equal(Object.hasOwn(error, "message"), false);
assert.equal(Object.hasOwn(error, "cause"), false);
assert.deepEqual(Reflect.ownKeys(error), ["stack"]);
const descriptor = Object.getOwnPropertyDescriptor(error, "stack");
console.log(
  JSON.stringify({
    keys: Object.keys(descriptor),
    get: typeof descriptor.get,
    set: typeof descriptor.set,
    enumerable: descriptor.enumerable,
    configurable: descriptor.configurable,
  })
);
assert.equal(Object.hasOwn(descriptor, "value"), false);
assert.equal(typeof descriptor.get, "function");
assert.equal(typeof descriptor.set, "function");
assert.equal(descriptor.enumerable, false);
assert.equal(descriptor.configurable, true);
assert.equal(Object.prototype.toString.call(error), "[object Error]");

const lookalike = Object.create(Error.prototype);
assert.equal(Object.getPrototypeOf(lookalike), Error.prototype);
assert.equal(Object.prototype.toString.call(lookalike), "[object Object]");
{
  class Error {}
  assert.notEqual(Error, Alias);
  assert.equal(new Error() instanceof Alias, false);
}
function Other() {}
const otherTarget = Reflect.construct(Alias, [], Other);
assert.equal(Object.getPrototypeOf(otherTarget), Other.prototype);
assert.equal(Object.prototype.toString.call(otherTarget), "[object Error]");

// V8's host hook can throw when the stack is read. Never replace it with a constant.
const previous = Object.getOwnPropertyDescriptor(Error, "prepareStackTrace");
const thrown = new Error("formatter threw");
try {
  let calls = 0;
  Error.prepareStackTrace = () => {
    calls++;
    throw thrown;
  };
  const delayed = new Error();
  assert.equal(calls, 0);
  assert.throws(
    () => delayed.stack,
    (reason) => reason === thrown
  );
  assert.equal(calls, 1);
} finally {
  if (previous) Object.defineProperty(Error, "prepareStackTrace", previous);
  else delete Error.prepareStackTrace;
}
