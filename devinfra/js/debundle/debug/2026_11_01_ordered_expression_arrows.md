# Expression-bodied arrows in the ordered executor

## Observed gap

The live-cell implementation instantiated ordered block bodies, but the
canonical arrow collector explicitly set `local_body` to absent for concise
expression bodies. Even this neutral closure factory therefore used the
source-summary path:

```js
const factory = (selected) => () => selected;
```

That path invalidates mutable-cell precision before flattening source effects.
A common loader's outer factory and simple captured readers cannot use ordered
cell semantics until concise bodies reach the same executor as block bodies.
Recognizing closure creation alone did not establish that coverage.

## Change and boundary

The canonical collector now lowers a concise arrow expression into one retained
return operation. Parameter validation, binding-target checks, free reads,
captured writes and allocation-only syntax classification use the same
finalization as block bodies. There is no synthetic AST, second traversal or
separate evaluator. The shared kernel executes that operation, instantiates
captured parameter cells and consumes call results through existing transfers.

Async/generator arrows, type annotations, defaults, rest, destructuring and
unsupported operators retain their existing conservative paths. Closure/call
operations stay outside strict allocation-only provider certification. Existing
direct-return proofs and foreign callback-wrapper derivation keep their
selection rules. No author premise or owned artifact format changes.

This supplies the outer factory and simple-reader prerequisite. Conditional
loader bodies, exact caller control, native/member results, getters and export
property origin still require work. No canonical gate acceptance is claimed.

## Review and tooling friction

Reviewed simple-parameter validation, concise return completion, shared
operation metadata, capture ownership and strict-provider routing. No tests,
builds, corpus runs, benchmarks or profiling jobs were added or run locally.
Normal hosted PR CI supplies existing checks.

**F17 — Callable body coverage:** the live-cell capability description was too
broad to reveal that concise arrows had no ordered body. When reporting missing
execution support, identify the original callable form and the first unsupported
operation, distinguishing absent syntax coverage from runtime refusal. Extend
the existing typed execution diagnostic at canonical body collection; do not
infer support from the presence of a callable or an ordinary completion result.
