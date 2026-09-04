import assert from "node:assert/strict";

import { remainingWaitMs } from "./deadline.mjs";

{
  // The whole derivation rests on Bazel putting the target's timeout in the test environment: if
  // that ever stops holding, every wait silently falls back to Puppeteer's default instead.
  assert.match(process.env.TEST_TIMEOUT, /^\d+$/);
}

{
  // A bound inside the declared timeout, with margin left for the failure to be reported before
  // Bazel kills the target — and enough of the timeout kept to be worth waiting.
  process.env.TEST_TIMEOUT = "300";
  const budget = remainingWaitMs();
  assert.ok(budget < 300_000, `${budget} must leave Bazel margin`);
  assert.ok(budget > 200_000, `${budget} must keep most of the declared timeout`);
}

{
  // A timeout too short to reserve against still yields a usable wait rather than a negative one.
  process.env.TEST_TIMEOUT = "1";
  assert.equal(remainingWaitMs(), 1_000);
}

{
  process.env.TEST_TIMEOUT = "not-a-number";
  assert.throws(remainingWaitMs, /TEST_TIMEOUT is not a positive number of seconds/);
}

{
  // Outside Bazel there is no declared deadline to derive from; callers fall back to Puppeteer's.
  delete process.env.TEST_TIMEOUT;
  assert.equal(remainingWaitMs(), undefined);
}

console.log("deadline.test.mjs passed");
