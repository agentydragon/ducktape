# Deferred class members and class creation completion

The existing original-syntax class creation proof excluded getters/setters,
private members and constructor parameter defaults. Their deferred operations
therefore made class declaration completion unknown even when the declaration
had no heritage, computed keys, decorators or static initialization. This was
a missing creation proof, separate from constructor or member invocation.

The proof now accepts JavaScript public/private methods and accessors, instance
fields and ordinary constructor parameters. Their bodies, defaults and instance
field initializers remain deferred. The existing exact source span, allocation
kind, owner and completed binding initialization checks still seal the receipt.
There is no new callable execution, summary or schedule exception. Heritage,
computed keys, static fields/blocks, decorators, auto-accessors and TypeScript
syntax remain outside this proof.

The moved-class fixture exports its preceding marker. This matters: a private,
uncaptured noninvoking marker can already move across an unknown owner under
the schedule guard, which would mask the missing class completion proof.
With the observable marker, the before-fix run
`4bf47ab5-fd85-4a6d-a231-a86627bb0906` rejects class owner 1 as
`evaluation_schedule_unknown_reordered`. With the proof, five deferred-member
variants move and preserve source/emitted Node output without executing their
throwing defaults, instance initializers or member bodies.

The separate host-setter fixture retains the captured class cell's TDZ phase.
The source setter calls the captured reader before class initialization and
observes `ReferenceError`; the diagnostic rejected output initializes the class
first and observes `ready`. Normal output remains refused. Normal creation
does not make captured/exported binding initialization unobservable.

The broader first run `9a0ccfa1-5514-42dd-be50-46aa1ec4ed64` passes the full
evaluation-schedule suite, the deferred-member positive and this TDZ control.
Local-class construction remains 14/21, realizability 27/28 and initialization
value-flow soundness 72/82, matching their preexisting failures. Three local-class
fixtures lack `opaque.js`; the other failures retain unknown host-operation or
unsupported syntax boundaries. Their old acceptance oracles were not changed.

Final bounded run `a11ea56a-47df-44d8-89d3-54d20f3d3f7e` passes both full
class-owner (7/7) and evaluation-schedule suites. Five eager heritage/computed/
static throw variants remain refused after crossing the exported marker; the
source executes each throw under Node. The existing ordinary construction,
field-invocation and class-binding TDZ controls also pass.
