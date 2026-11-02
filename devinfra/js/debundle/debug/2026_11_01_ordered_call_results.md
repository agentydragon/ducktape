# Ordered call results

## Missing operation and generic reproduction

The ordered executor could assign and read local records but could not consume
the result of an invocation at its source position. For example:

```js
function select(input) {
  return input;
}
function build(select, input) {
  const record = { value: select(input) };
  return record;
}
```

The nested call inside that object literal still needs recursive ordered
literal construction; the first supported form makes the call explicit:

```js
function build(select, input) {
  const value = select(input);
  const record = { value };
  return record;
}
```

The source collector now retains direct calls through simple binding references
and their ordered arguments. The shared kernel reads the callee first, then
each argument from left to right. Argument assignments cannot replace the
already selected callee. Abrupt operands stop before invocation, and a possible
abrupt invocation result remains in the frame's result.

## One outcome, with existing safety boundaries

The application adapter invokes one singleton untainted local function or
closure through the existing invocation transfer. It consumes both effects and
return values from that outcome, rather than evaluating the call separately to
obtain its value. Known effects, including rebinds and proof dependencies,
follow the same merge as the ordinary call adapter. Results stay opaque to the
allocation-only judgment. Strict provider queries decline this operation.

Objects reachable through the callee or arguments become borrowed in the
caller before the invocation. This prevents retaining a fresh own-data
descriptor proof across a callee that changes that object. Returned
capabilities remain fallback roots even if their receiving binding is later
overwritten. Unknown targets and incomplete invocations truncate the body at
the source call site through the established bounded callback fallback. They
do not replay the invocation or preceding operations.

Member, optional, spread and direct eval calls remain outside this grammar.
Native and foreign call certification, live captured cells, branch execution,
facade getters and property-origin export identity remain required work. This
change alone does not prove the canonical provider or clear any gate.

## Review and friction

Reviewed the canonical lowering and free-read collection, operand order,
completion handling, effect/result transport, caller freshness revocation and
fallback roots. No tests, builds, corpus runs, benchmarks or profiling jobs
were added or run locally. Normal hosted PR CI supplies existing checks.

**F15 — Effect and value paths for calls:** the general application evaluator
still has separate call-effect and call-value paths. Stateful native models,
getters and live-cell factories need one shared result carrying both effects
and value before entering this ordered path. Re-evaluating an already executed
call is not a safe way to recover its result. Extend the common invocation
result rather than introducing a provider-specific evaluator.
