// Observable JS behavior for the string-key branches of a lowered public field.
import assert from "node:assert/strict";

function publicField(define, target, key, value) {
  if (key in target) define(target, key, { enumerable: true, configurable: true, writable: true, value });
  else target[key] = value;
  return value;
}

class Parent {
  static get cache() {
    assert.fail("an own data definition must not read the inherited accessor");
  }
  static set cache(_) {
    assert.fail("an own data definition must not invoke the inherited setter");
  }
}

for (const target of [class {}, class extends Parent {}]) {
  const alias = target;
  const map = new WeakMap();
  const other = new WeakMap();
  assert.equal(publicField(Object.defineProperty, target, "cache", map), map);
  assert.deepEqual(Object.getOwnPropertyDescriptor(alias, "cache"), {
    value: map,
    enumerable: true,
    configurable: true,
    writable: true,
  });
  assert.equal(alias.cache, map);
  assert.notEqual(map, other);
  map.set(target, 7);
  assert.equal(WeakMap.prototype.get.call(alias.cache, alias), 7);
  assert.equal(other.has(alias), false);
  assert.throws(() => WeakMap.prototype.get.call(Object.create(WeakMap.prototype), alias), TypeError);
}

// Move only field initialization across one step. Each run starts with a new
// class, constructor binding and log. The surrounding realm is unchanged.
function run(early, setup) {
  const target = class extends Parent {};
  const host = { WeakMap };
  const trace = [];
  let phase = 0;
  const observe = () => trace.push(phase);
  const { define = Object.defineProperty, replace = () => {} } = setup(host, observe);
  const cross = () => {
    phase = 1;
    replace();
  };
  const init = () => publicField(define, target, "cache", new host.WeakMap());
  if (early) init();
  cross();
  if (!early) init();
  return { trace, cache: target.cache };
}

for (const setup of [
  (host, observe) => {
    Object.defineProperty(host, "WeakMap", {
      get() {
        observe();
        return WeakMap;
      },
    });
    return {};
  },
  (host, observe) => {
    host.WeakMap = class extends WeakMap {
      constructor() {
        super();
        observe();
      }
    };
    return {};
  },
  (_host, observe) => ({
    define(...args) {
      observe();
      return Object.defineProperty(...args);
    },
  }),
]) {
  assert.deepEqual(run(false, setup).trace, [1]);
  assert.deepEqual(run(true, setup).trace, [0]);
}

const shared = {};
const replacement = (host) => ({
  replace() {
    host.WeakMap = class {
      constructor() {
        return shared;
      }
    };
  },
});
assert.equal(run(false, replacement).cache, shared);
assert.notEqual(run(true, replacement).cache, shared);
