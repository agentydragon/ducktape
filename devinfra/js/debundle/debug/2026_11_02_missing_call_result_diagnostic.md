# Ordered calls without a result transfer

## F26: report the missing transfer

An ordered body can reach a call such as:

```javascript
function read() {
  return provider(1);
}
```

Suppose an existing audited provider contract discharges synchronous reentry
but supplies no result model. The call dispatcher returns its effects without
a value. The ordered adapter then truncates the body and reports generic
`unsupported_operation` / `opaque_execution`. If the callee's abstract value is
unknown, it also reports `unknown_call_target` despite the consumed timing
proof. That directs investigation toward the callee or another timing premise
when the missing implementation is the result transfer.

The dispatcher now distinguishes an effects-only transfer, a returned value,
and refusal of the already evaluated operands. A complete effects-only
transfer produces `missing_call_result`. The body still takes the same
conservative fallback, retaining evaluated operands, partial capabilities and
source location. No source evaluation is retried. Native and foreign result
projections remain lazy for the effects-only consumer.

## F28: an unchanged completeness flag is not admission

The ordered dispatcher can decline a non-singleton, tainted or otherwise
unsupported local callee before executing its effect transfer. Its initial
outcome still says `complete`. Treating every absent value with that flag as
a timing proof would mislabel these cases.

Keep this refusal explicit in the transient transfer result. Only the
effects-only branch plus a complete outcome establishes `missing_call_result`;
the refused branch retains the opaque-operation fallback and unknown-target
record when applicable. Future transfer paths that decline before modelling
effects must use the same refusal branch. The enum is internal runtime state,
not a saved-format compatibility layer.

## F29: an opaque-call hint can send the author toward a premise

An existing private CI log repeatedly suggests `purity: pure` or
`fluent_exports` for opaque initializers. `purity/purity_type.rs` emits this
generic hint without establishing either contract's prerequisites. Incomplete
interpreter support can produce the same visible symptom. Call and constructor
guidance now directs authors to the source and synchronous precision loss before
choosing an audited contract. It names the assertion's limits: missing results,
instance shape and heap identity require their own proof. A diagnostic is not
evidence that a contract applies.

## Review and remaining work

Source review traced every dispatcher exit, its outcome update, the ordered
consumer and the effects-only consumer. The shared fallback's havoc,
publication, callback reach and returned capabilities are unchanged; its
diagnostic reason and assumed-effect provenance become specific. The example
above describes the source path; no local test or corpus run was performed.
Existing normal hosted PR CI supplies compilation and regression checks.

Array/from-entries helpers, general root control and cross-chunk provider
execution still need operand/result integration. This diagnostic grants no new
premise, allocation proof or gate acceptance. Saved graphs remain gzip JSON
with the current typed schema; no legacy decoder or alternate encoding is added.
