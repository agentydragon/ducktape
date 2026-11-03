# Function creation with lazy parameters

The original-owner initializer certificate admitted function expressions and
arrows only with simple identifier parameters. It also excluded async and
generator functions. This left global, heap and host coverage unknown for a
function value whose creation evaluates neither its parameters nor its body.

A neutral CLI fixture extracts an arrow with a default value, destructured
parameters and rest parameters while keeping an independent primitive
initializer residual. The emitted import moves function creation before that
initializer. The report must show the actual swapped pair without havoc;
only the output call remains uncovered. Calls and global reads in the default
and body stay lazy, so the fixture can run without supplying those globals.

A separate invocation control reads through an argument getter. Its runtime
output is preserved, and the call owner remains uncovered. The creation
certificate establishes no safety or timing fact about later invocation.

The existing exact original owner, allocation identity, dedicated initializer
transfer and completed binding-initialization checks still seal the
certificate. No generic closure value, purity result or child ledger grants
whole-owner coverage.

Inspection friction: an exact source-unit inspection of a comma-split
declarator displays its original multi-declarator statement. Its sibling
bindings are context, not additional selected owners. Use the native selected
unit's declared binding and span when identifying the initializer.
