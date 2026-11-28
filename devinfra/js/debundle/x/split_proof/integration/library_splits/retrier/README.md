# Retrier baseline

The input is the complete public ESM entry, `dist/retrier.js`, from
`@humanwhocodes/retry` 0.4.3, already pinned in the root lockfile. The test
checks the package export and the file hash. The authored plan keeps the
public class and its export together and puts the constants, helper functions,
and internal `RetryTask` class in a dependency. All ten source units retain
their order.

Run:

```sh
bbr test --test_output=all //devinfra/js/debundle/x/split_proof/integration/library_splits:retrier_baseline_test
```

The ordinary `lowersplit --require all-outcomes` constructor now publishes
this exact placement and independently verifies it with `--check-output`.
Both class bodies are retained as original syntax. The constructor requires
the same linked statements in the same order, identical captured cells and
public bindings, and rechecks those facts on the parsed output. The receipt
covers the modeled module graph and leaves deployment and client coverage
unclaimed. A duplicated source row or an altered on-disk export is rejected.

In Node on the original and the published files, the public `retry` method fulfills a simple task with 42.
The discriminator holds an async task at an explicit promise with concurrency
one. One job is working and one is pending. The held task writes before
throwing, the retry predicate sees the same error and declines it, and the
second job starts, writes, and fulfills with 7. Both queues and the working
count are then zero. A controlled replacement for the retrieved `shift` method sees the original
queue, returns its first callback by identity, and sees the pending count
drop before the callback runs. This is a Node observation, separate from
the constructor's same-order proof. The timer path is untested.

The separate concrete classifier and concrete decoder still first refuse unit 7: `unsupported global Math`. It comes from
`RetryTask.id = Math.random().toString(36).slice(2)`, even though the observed
non-retry consumer never constructs a `RetryTask`. A separate direct-unit
diagnostic exposes earlier bodies that cannot be lowered directly, including
the ambient `globalThis` root in `debug`, `Date` in `isTimeToRetry`, and
`Promise` in `createPromise`. The optional member tail now lowers when its
root is a modeled lexical value. Those function declarations can be deferred in the linked
classifier; the direct-unit verdicts are not the ordinary CLI verdict or
proofs of the deferred bodies. The Node observations supply no host or
checkpoint premises to the splitter.

## Synchronous pending receiver

`retrier_pending_test` extracts the unchanged `#processPending` method,
the `pending` and `working` getters, and `this.#working++` from the pinned
source. The projection supplies a lexical `Math.min`, a counting `debug`
function, and an ordinary queue with its own `shift` method. A test-only
entry method calls the private method, and a detached pending arrow invokes
a small private job method on the original Retrier. The job increments
the counter and calls the supplied callback. The original async job body
is outside this projection.

Previously CoreJS deferred the private member update with
`expected an identifier` and the lexical `task?.()` call with
`unsupported optional call`. The lowerer now reuses the ordinary update
operation between a private read and write on the same evaluated receiver.
The optional call reads the lexical binding once, skips only when nullish,
and calls a present value without a property receiver.

CoreJS and Node agree that the detached job sees the original receiver and
the incremented working count. If its callback throws, the exact sentinel
escapes, the count stays incremented, and the second job stays queued.
The private method and the original getter both throw a type error on an
unbranded receiver. The model observes the error kind directly; reading
properties of modeled engine errors is outside this test.

With an actual Array queue, Node completes the same method and moves the
counts to `[1, 0]`; CoreJS stops at `host function Array.prototype.shift
is called`. The full ESM package still refuses at its independent linked
classification boundary. The projection supplies no Promise, timer, or
pristine-global premise and makes no artifact publication claim.
