# Primitive await and split observations

The shared split claim, `LinkedBehavior.ScheduleEquivalent .allOutcomes`,
compares `runProgram` at each fuel bound. It includes normal, throw, unknown
and fuel outcomes, and the entire state for outcomes that carry one.
`SameExports` separately checks how the actual linked names resolve to live
cells. The linked await entry point uses the same linker and explicitly
delegates a module without await cuts to `runUnits`. For a successful
singleton link, `LinkedPrimitiveAwait.noCuts_agree` proves exact erasure to
`runProgram`, including throws and unknown reasons, and an empty queue in
every stateful outcome. It does not extend the current split claim to a
module that suspends.

The overlap exposed a fuel mismatch. With fuel 4,
`async function f() { await 0; } f();` used to enqueue before the shared
evaluator had enough fuel even to evaluate the expression in
`async function f() { 0; } f();`. Evaluating the await operand now consumes the
same list layers for preceding statements and the same statement layer as
an ordinary expression. The control reaches the cut at fuel 6.

The proof for a reached await needs the following observations in the shared
execution result:

- At the end of module evaluation, compare the ordinary result and its
  state **and** the ordered pending continuations. A thrown module can
  leave jobs queued, and an unknown result can carry earlier queued jobs.
  Fuel exhaustion has no recoverable state in the current semantics.
- A continuation must identify its pending Promise, its live captured cells
  and receiver, and the remaining code and allocation region. Queue entries
  must agree in order modulo the state and code correspondence used to
  compare the two linked graphs. Equal visible heap values alone cannot
  establish equal results after resumption.
- Compare the outcomes of **each explicit drain quota** from matching
  states and queues. This includes a paused queue, continuation writes and
  output, fulfillment or rejection on the original Promise, a remaining
  queue on an unsupported result, and bounded evaluator fuel. A language
  rejection settles one Promise; it does not terminate the drain.
- The certificate needs to decode the actual published output and connect
  its resumed code and captures to the source. Comparing a hand-written
  candidate, draining between statements, or projecting away the queue
  cannot establish that certificate.

The placement test moves only the declarations and their live cells into a
dependency. In Node, the source and that physical placement both observe
an empty trace in the module and `A7B7` after the jobs run. Swapping the two
call sites has the same in-turn observation and produces `B7A7` instead.
The linked differential also refuses an object operand that would need
thenable assimilation. The placement test requires the production splitter
to refuse the authored plan without publishing files. Await admission
and an end-to-end certificate for an emitted placement are future work.

`PrimitiveAwait.drain_corresponds` now gives the drain part of this claim
from equal immediate states and ordered corresponding jobs. A corresponding
job has the same remaining statement list, allocation region and Promise
address. The shared scope theorem requires the same receiver and home object,
and equality of the binding addresses used by the remaining code. The
enqueue rule retains those addresses, not snapshots of their values.
At every quota and evaluator bound, the drain lemma preserves the outcome,
complete state (including writes before a rejection or refusal), and any
remaining ordered queue.

The staged-link test passes the reread programs to
`checkStagedImmediate`. The checker verifies the shared link state, the
public export addresses, and the ordered unit IDs, statements, and
free-name binding addresses. It uses `link_from_probe` to transport only
the links to every world. The preservation proof then works at arbitrary
fuel using the shared scope equations for ordinary statements, the direct
callee and arguments, and the destination binding. Checked original
declaration IDs give both sides exactly the same cut table.

This gives exact immediate outcomes and pending queues for the preserving
placement, including the variant that throws with both jobs pending.
Every chosen explicit drain therefore also agrees. Swapping the calls
fails the ordered linked-unit check despite the equal immediate state
observed in the probe; the resumed trace changes from `A7B7` to `B7A7`.
Moving only the captured `trace` binding is refused because the original
assignment would target an import.

The staging callback always refuses publication. The checker proves an
all-world, all-fuel claim about the explicit primitive-await semantics and
the actual reread files. The ordinary projection still runs unlowered
programs with the same semantics and fuel.

The staged payload now retains the exact deferred function lowered at the
original unit. The reread declaration must match that function, including
its callable site and capture list. Binding the payload checks the concrete
closure in the original allocation region; invocation checks its address,
deferred function and linked environment. A reassigned name cannot lend
the original cut to another function. On that path the ordinary callee
runs at the same bound, including the existing refusal for unrelated
deferred code.

The source fixture with a getter operand uses the shared property rule for
that read. Suspension appends after the operand's returned turn. The module
step keeps that turn for the next unit, and the drain takes its next state
and FIFO from the resumed computation. A quota-one check on the staged
pair observes the first write before its rejection, with the second Promise
still pending. Resumed code and other operands still use the ordinary
rules for the supported fragment; a nested deferred suspension, unmodeled
thenable or imported capture remains outside it.

`TurnProgram` carries the executable suspension bodies as data from checked
lowering. The fixed turn evaluator runs the same linked schedule used by
staging. Ordinary programs embed without suspension bodies; the existing
ordinary projection theorem still covers those programs. On the original
fixture at fuel 3, the ordinary embedding refuses the deferred function
`left` with an empty queue, while the lowered program exhausts fuel trying
to reach the operand. At fuel 7, the lowered program has two pending jobs.

`checkProductionBridges` uses the exact decoded pair and checked links to
prove both full-turn equalities for every world and fuel. The publication
interface also requires a positive language completion and successful
empty drain in every world. The staging callback still refuses publication.
No ambient global policy is needed.
