# Ordered record operands

## Missing composition

The ordered kernel could consume `const value = select(input); return { value }`
but rejected `return { value: select(input) }`. Record values lowered through
`ValueExpr` could not retain the call's ordered effect/result operation.
Nested local assignments, closures and field reads hit the same boundary.

Canonical local-body lowering now retains recursive expressions for static
data fields. The shared kernel evaluates each child once and feeds its value
and judgment into the existing record allocation transfer. General application
and strict provider literals use that same transfer. All child read/write and
allocation-syntax censuses recurse through the retained operations.

Calls and closures retain their established opaque allocation judgment; this
does not certify a provider or add native, member-call or branch semantics.
Computed keys, spreads, accessors and prototype-setting fields stay unsupported.
Duplicate fields retain every operand's effects and the existing heap joins.

## F21: unfinished literals need capability roots

A later child can trigger fallback before the outer record has entered the
heap. Already evaluated field values must then remain reachable even if their
local aliases change. The frame retains these values as fallback inputs.
An abrupt child stops later fields; a failed child never causes a retry of the
literal or its earlier operands.

A child can also reenter the same source allocation site. A heap entry created
by that child represents an older record too, so the outer result cannot gain
fresh-record admission merely because the site was absent before evaluation.
The kernel checks this before completing the literal.

## Review

Reviewed operand order, abrupt/fallback paths, partial-literal roots, recursive
capture/read/write censuses, duplicate keys and allocation-site reentry. No
tests, builds, corpus runs, profiling or benchmarks were added or run locally.
Normal hosted PR CI supplies existing checks. No canonical gate acceptance or
foreign-provider identity is claimed.
