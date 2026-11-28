// This driver is only used by the isolated source projection. Neither
// its synthetic callbacks nor the projected private loader are public API.
import assert from "node:assert/strict";

export function observe(factory, loadConstants) {
  const constants = loadConstants();
  assert.equal(constants.SEMVER_SPEC_VERSION, "2.0.0");
  assert.equal(loadConstants(), constants);

  const exception = new Error("callback failed");
  const attempts = [];
  let load;
  load = factory({
    entry(exports, module) {
      assert.equal(this, undefined);
      assert.equal(exports, module.exports);
      const ordinal = attempts.length + 1;
      exports.ordinal = ordinal;
      attempts.push({ exports, module });
      assert.equal(load(), exports);
      if (ordinal === 1) throw exception;
      module.exports = { completed: exports };
    },
  });
  assert.equal(attempts.length, 0);
  assert.throws(load, (error) => error === exception);
  assert.equal(attempts.length, 1);
  assert.equal(attempts[0].exports.ordinal, 1);
  const second = load();
  assert.equal(attempts.length, 2);
  assert.notEqual(attempts[0].module, attempts[1].module);
  assert.notEqual(attempts[0].exports, attempts[1].exports);
  assert.equal(second.completed, attempts[1].exports);
  assert.equal(second, attempts[1].module.exports);
  assert.equal(load(), second);
  assert.equal(attempts.length, 2);

  console.log(
    JSON.stringify({
      constantsVersion: constants.SEMVER_SPEC_VERSION,
      callsOnAllocation: 0,
      callbackAttempts: attempts.length,
      reentryReadsPartialExports: true,
      throwLeavesExportWrite: true,
      retryAllocatesDistinctModuleAndExports: true,
      cacheHitReturnsCurrentExports: true,
    })
  );
}
