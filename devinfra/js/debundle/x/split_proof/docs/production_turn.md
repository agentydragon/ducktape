# A queue-aware boundary for the production evaluator

The production constructors prove facts about CoreJS programs, then the stage
gate checks that the independently read files decode to the constructed program.
Keep that division: syntax and placement evidence belongs in the constructor;
neither a constructor nor a certificate passes an evaluation mode to the
interpreter.

## First slice

`Turn W` is the full CoreJS state and the FIFO. A job keeps the original
allocation region, Promise address, and remaining code with its live binding
addresses. `TurnResult W α` has the existing success, throw, unknown, and
fuel cases. A throw or unknown keeps its turn; fuel still carries no state
or queue. The explicit scheduler reports empty or paused and can refuse or run
out of fuel; a language throw from a job rejects its Promise.

`runProgramTurn w fuel program` currently uses the real linker and
`runUnits`. It shares the link boundary, outcome type, and explicit checkpoint
observation with the staged await model. It has no enqueue rule yet.
`runProgramTurn_sync` proves the full lift of the current `runProgram`,
with an empty FIFO wherever execution returns a state, for every program,
world and bound, including failed links. `runProgram_eq_erase` recovers the
old result with no fuel translation. Keep the old definition and its
reduction lemmas during this slice.

`TurnPublication` is indexed by the source and actual generated program.
It requires:

1. The same public binding addresses.
2. Equal immediate outcomes, including the entire FIFO, at each common fuel
   bound in every world. By projection this implies the ordinary all-outcomes
   theorem. Applying the same explicit checkpoint to both turns gives
   equality for any common job fuel and quota, including refusal and the
   remaining tail.
3. A positive language completion (return or throw) and a successfully emptied
   queue at some finite bounds in each world. Two unknowns, even if they have
   the same reason and queue, cannot serve as this witness. The witness proves
   support; it does not choose checkpoint policy or prove a host checkpoint.

The literal-reorder constructor is the migration check. Its existing proof gives
the ordinary result equality, the current kernel supplies the empty-queue
lift, and two fuel levels suffice for a positive completion in any world.
The readback adapter uses the same artifact identity check and proves the
same acceptance decision for any files once this semantic proof is supplied.
The FIFO requirement is observable: two results with identical ordinary
state can have an empty versus a paused checkpoint even at quota zero.

## Before adding suspension to production

Put the new behavior at the existing call dispatch on a positively lowered
closure, including alias and nested calls through that dispatch. Do not
register source-side cuts next to a certificate, intercept only syntactic
top-level calls, or substitute the old evaluator when a constructor asks.
Retain the existing deferred and host-backed behavior wherever lowering
cannot authenticate the new closure and suspension point. Reread must
reconstruct that same lowered code with the same binding targets. In
particular, today a reached deferred body can return unknown. Extending it
to suspend intentionally changes that case, and matching deferrals are no
evidence for the new rule.

Prove the compatibility equation at unchanged fuel on the overlap, using
a property of the execution (or a syntax proof of it) that the new equation
cannot be reached. A shallow check for no top-level await calls is
insufficient: call dispatch occurs under accessors, nested evaluation and
host re-entry. Existing certificates keep their stated strength, including
the distinction between exact bounded correspondence and normal-completion
equivalence. To migrate other publishing rules, prove the overlap and queue
property on their admitted programs before changing their gate. A rule that
only proved ordinary equality must not inherit queue equality after
suspension becomes possible; a normal-only rule must not inherit an
all-outcomes claim. The positive support requirement needs a separate proof
where a rule assumes additional facts about a world.

Keeping jobs next to the language state preserves the shape of `State`,
projections, and heap/cell lemmas. Threading that sidecar through call effects
will require lifting sequencing, unwinding and callback operations along
with their equations. Putting the queue in `State` would thread it through
the existing monad automatically but would change the meaning of its
state equality and frame lemmas wholesale. The next decision is how to
thread the queue through the shared call kernel, after one overlap proof
through a real nontrivial caller. The staged await prototype is still a
separate diagnostic; its proof is not a publishing proof for that kernel.

## Shared resolved call dispatch

Both observations now use one dispatch table on the resolved callee value.
Alias lookup still happens in ordinary expression evaluation. The selected
local closure runs the same body at the already reduced fuel; a synchronous
async body still settles its Promise by the existing rule. A nested call
enters the same table. `callFnTurn_overlap` frames any incoming FIFO around
the complete old outcome, at identical fuel and for arbitrary worlds.
`evalCall_turn` exposes that boundary after callee and argument evaluation.
`execFinally_turn` passes the post-body turn to the finalizer after a return
or throw. Its own jump or throw still wins; an unknown or fuel outcome still
does not run the finalizer.

Host behavior is unchanged. The existing argument and handle checks run
before the corresponding host operation. A host step or conversation that
updates its state, hands out an identity, or re-enters a closure keeps those
effects even if it later refuses. A request with an unissued handle stops
before calling it. `performCall_turn` exposes the shared dispatch in the
callback request; `TurnM.attempt` catches a language throw on the resulting
turn, without making unknown or fuel catchable.

The dispatcher currently lifts the old local body and opaque conversation
effects, which cannot enqueue. A queue-producing rule must instead thread
the turn through bodies, host callbacks, and any other path that can re-enter
the evaluator. In particular, wrapping a queue-producing call in the current
`TurnM.lift` would lose the jobs; this slice neither adds such a rule nor
changes the publishing gate.

## Passing an updated turn through recursive rules

The ordinary evaluator now calls shared rules for direct invocation, entering
and completing a local body, statement-list continuation, cleanup, and a step
of the modeled host conversation. Each rule accepts its recursive computation
at the same fuel as before. At these boundaries the turn-valued instances
pass the resulting turn to the next statement, the caller, the finalizer, or
the next host step. The corresponding Lean equations quantify over the whole
incoming FIFO and recover the ordinary result at the same bound.

The regression starts with two queued markers. It adds a third marker once,
after an actual ordinary statement in an inner body. Nested return and throw
keep all three in order through the finalizer. Model refusal keeps all three
and skips the finalizer. A modeled host callback also keeps all three through
the value reply and a subsequent host refusal. The test composes the shared
rules at the recursive edges of fixed fixtures. It does not install a new
JavaScript operation or let a host enqueue a job.

There are still recursive edges that cannot carry an updated queue. Reading an
accessor through `getProp` uses the existing state-only `Call` result. The
getter fixture returns the same value and logs the same line through that
interface, but the third marker is lost. The next change at this edge is to
make the property-access rule accept and return the same effect as the
callback, and then thread it through `getProp` and the modeled host `.get`
request. Similar callback-bearing paths need their own overlap proofs (for
example setters, constructors, and iterators). The shared recursion interfaces
here do not authorize projecting a queue-bearing operation through any of
those paths. The production evaluator still has no enqueue rule, and its
publication gate is unchanged.
