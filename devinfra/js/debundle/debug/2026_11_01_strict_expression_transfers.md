# Shared strict expression transfers

The existing ValueExpr lowering now retains strict-equality operands, a void
operand, and the test plus arms of a conditional. Only ===, !==, void and
conditional syntax join the existing narrow source coverage; unsupported
children cannot reset a failed coverage flag. Both equality forms abstract to
an unknown Boolean and prove no runtime identity.

The shared allocation transfer evaluates equality operands from left to right,
evaluates and discards a void operand, and joins terminal conditional result
arms without truthiness refinement. It preserves child access evidence and
possible abrupt outcomes. Support is selected before evaluating children, so
application effects are never executed and then rolled back for a fallback.
General conditional branches retain ordinary conservative execution, now with
their previously discarded test. No general branch-state/source-order proof or
execution purity certificate is added.

The strict provider query can derive an ordinary record whose fields retain
input and a conditionally normalized comparison input. Its union retains both
possible result arguments; arguments read only by equality, void or a test do
not become result roots. Full actual importer Values remain in the existing
opaque result carrier. No object shape, freshness, privacy or equality is
inferred from that carrier.

Public CLI controls check Node output, consumed foreign summaries, retained
arguments and observed coercion/getter/callback effects and throws. Direct
function and const-arrow queries prove the new path without an old evaluator
fallback. Existing literal migration and callback fixture families remain
validation requirements. These are feature and preservation controls, not a
claim of a demonstrated baseline runtime regression or corpus precision gain.

Closed provider captures and stable namespace member identity remain separate
requirements. This change does not admit the actual vendor memo member, change
the callback premise or enable a private spec opt-in.
