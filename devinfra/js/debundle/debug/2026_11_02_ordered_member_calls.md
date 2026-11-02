# Ordered member-call operands

## Missing operation

An ordered body cannot consume `Object.getOwnPropertyDescriptors(input)`
through the shared call dispatcher while its callee grammar accepts only a
binding. This blocks composition of existing native descriptor proofs in
source-covered function bodies.

A member call also retains a receiver before its arguments run:

```javascript
function read(receiver, replacement) {
  return receiver.select((receiver = replacement));
}
```

Both the selected function and its implicit receiver come from the original
reference. Reading the receiver binding after the assignment chooses another
object. The receiver and completed operands also remain capabilities of a
truncated remainder.

## F30: receiver identity includes implicit captures

Allowing a local method to return a closure does not establish that the
closure is independent of `this`. An arrow can retain the method receiver
without an identifier capture. The interpreter does not yet transport that
implicit capture. Reuse the existing unused-receiver check for local method
results, including nested callable implicit-capture flags. Retain the original
receiver on every conservative fallback. Native rows still require their
existing exact identity and operand proofs.

## F31: body admission can hide a supported operation

The ordered invocation's free-read check originally admitted only an available
capture or a declared chunk binding. A global native reference such as `Object`
matched neither condition, so adding its member-call operation alone would
leave the whole body on the source-summary path. Inspect entry checks as well
as operation transfers when a supported form appears inert.

Ordered application admission now also accepts an unshadowed, undeclared native
global whose global slot passes the existing native-integrity guard. This
records the native proof's dependencies. Other missing reads keep their
conservative path, and method identity and invocation inputs still require
their separate checks. A familiar global name supplies no proof on its own.

## F34: lookup and result transport must migrate together

Normal hosted CI at `9ac304811c82b2d74132f1017dd5f71a6e21c9b5` rejected the
existing `descriptor_inspection_preserves_eager_argument_effects` fixture.
Its argument helper calls `effects.push('argument')` before returning a
primitive. Entering the new ordered member-call path lost the source-summary
push proof, making descriptor capture conservative and introducing a rejected
atomic group. The failing test was read from BuildBuddy
`0264a642-c1ee-5688-be71-7f84a90a67df`; no local test was run.

The existing push receiver guard now serves both callee lookup and invocation.
It retains singleton ordinary-array identity, settled descriptors and the same
Array/Object prototype and iterator integrity requirements. The invocation
consumes the captured receiver and already evaluated primitive arguments. It
keeps the existing invalidation of precise contents and returns a numeric
completing value plus a possible throw for length overflow. It does not infer
an exact length or mutate the literal snapshot to simulate another activation.
Other array operations still require their own operand/result transfer.

## Transfer and review boundary

The canonical local body IR retains static members of binding receivers. The
shared kernel reads the receiver, looks up its callee, then evaluates arguments
once from left to right. Receiver, callee and completed arguments remain frame
capabilities if a later operation truncates. Ordinary field reads and call
lookups share one fresh own-data transfer. The application adapter also reuses
the canonical native or namespace lookup certificate at its original source
site; an unproved getter stops before argument evaluation.

`CallOperands` transports a typed evaluated callee reference to the existing
dispatcher. Native results still come from its checked native resolution. A
local method result needs the existing unused-receiver proof in addition to
singleton and taint checks. The effects-only source-summary consumer keeps its
established receiver behavior. The prepared invocation and its fallback use
the receiver snapshot instead of reading a possibly rebound source binding.
Strict provider queries still refuse every invocation.

Reviewed source lowering, native-global entry admission, free-read/write coverage, operand order, shared
field guards, canonical lookup policy, native input checks, partial-operand
retention and receiver-dependent closure refusal. The examples describe the
source path; no tests were added or run locally and no local build or corpus
run was performed. Normal hosted PR CI supplies existing checks.

Actual `this` binding, getter execution/results, computed receiver evaluation,
array iteration result transport, ordered root control and shared foreign
activation state remain semantic work. This change grants no allocation-only
certificate, premise or canonical gate acceptance. Saved graphs retain only
the current gzip JSON format.
