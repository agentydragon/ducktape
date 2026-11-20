import Std

/-!
A transition model for a factory-created closure and an unrelated Set allocation.
This is not ECMAScript semantics. The factory's private parameter is immutable in
this subset; its stored integer abstracts the identity of the function argument.
The body is deferred, and invoking it is modeled only as observing that identity.
-/

namespace FreshClosure

structure ClosureId where
  value : Nat
  deriving DecidableEq, Repr

structure SetId where
  value : Nat
  deriving DecidableEq, Repr

inductive Event where
  | log (value : Int)
  | escaped (id : Option ClosureId)
  | invoked (capturedArgument : Option Int)
  deriving DecidableEq, Repr

structure World where
  /-- The current value of the factory argument binding. -/
  argument : Int
  /-- A closure's private immutable parameter contains the supplied argument. -/
  closures : ClosureId → Option Int
  /-- The factory result is private until an explicit publication or call. -/
  result : Option ClosureId
  nextClosure : ClosureId
  /-- Set and closure identities have separate types: neither is an observable
  numeric address, and a Set can never be identical to a closure. -/
  setResult : Option SetId
  sets : SetId → Bool
  nextSet : SetId
  events : List Event

def update {κ α : Type} [DecidableEq κ] (f : κ → α) (key : κ) (value : α) : κ → α :=
  fun x => if x = key then value else f x

/-- The factory evaluates its argument now, creates a fresh private parameter
cell and closure, then initializes the result. No deferred body runs here. -/
def make (w : World) : World :=
  { w with closures := update w.closures w.nextClosure (some w.argument), result := some w.nextClosure, nextClosure := ⟨w.nextClosure.value + 1⟩ }

inductive Stmt where
  | log (value : Int)
  | writeArgument (value : Int)
  | pureNewSet
  | publishResult
  | invokeResult
  /-- A hostile replacement constructor invokes a callback before returning. -/
  | reentrantNewSet
  | allocateClosure
  deriving DecidableEq, Repr

def invoke (w : World) : Option Int := do
  let id ← w.result
  w.closures id

/-- `pureNewSet` is a semantic primitive: a successful intrinsic Set allocation
with no callbacks or other effects. Nothing infers that premise from JavaScript. -/
def step (s : Stmt) (w : World) : World :=
  match s with
  | .log value => { w with events := w.events ++ [.log value] }
  | .writeArgument value => { w with argument := value }
  | .pureNewSet => { w with setResult := some w.nextSet, sets := update w.sets w.nextSet true, nextSet := ⟨w.nextSet.value + 1⟩ }
  | .publishResult => { w with events := w.events ++ [.escaped w.result] }
  | .invokeResult => { w with events := w.events ++ [.invoked (invoke w)] }
  | .reentrantNewSet => { w with setResult := some w.nextSet, sets := update w.sets w.nextSet true, nextSet := ⟨w.nextSet.value + 1⟩, events := w.events ++ [.invoked (invoke w)] }
  | .allocateClosure => { w with closures := update w.closures w.nextClosure (some 0), nextClosure := ⟨w.nextClosure.value + 1⟩ }

/-- Syntax-directed sufficient condition. Escape, invocation, a reentrant
constructor, argument changes, and competing closure allocations are refused. -/
def commutable : Stmt → Bool
  | .log _ | .pureNewSet => true
  | .writeArgument _ | .publishResult | .invokeResult | .reentrantNewSet | .allocateClosure => false

/-- Under the syntax check the entire modeled state agrees, including typed
identities, captured argument, both output slots, counters, and trace. -/
theorem make_step_commute (s : Stmt) (w : World)
    (h : commutable s = true) : step s (make w) = make (step s w) := by
  cases s <;> simp_all [commutable, step, make]

/-- A continuation may publish or invoke the closure and allocate either kind
of identity. This is stronger than equality of the traces seen so far. -/
def run (stmts : List Stmt) (w : World) : World := stmts.foldl (fun state stmt => step stmt state) w

theorem inferred_swap_preserves_continuation (s : Stmt) (w : World)
    (continuation : List Stmt) (h : commutable s = true) :
    run continuation (step s (make w)) = run continuation (make (step s w)) := by
  rw [make_step_commute s w h]

/-- If the next identity was not already allocated, the new closure occupies a
fresh identity. A surrounding semantics must maintain this invariant. -/
theorem allocated_closure_fresh (w : World) (h : w.closures w.nextClosure = none) :
    w.closures w.nextClosure = none ∧ (make w).closures w.nextClosure = some w.argument := by
  simp [h, make, update]

/-- Set freshness also requires an unused next identity; incrementing the
counter alone does not prove the surrounding allocator has maintained it. -/
theorem allocated_set_fresh (w : World) (h : w.sets w.nextSet = false) :
    w.sets w.nextSet = false ∧ (step .pureNewSet w).sets w.nextSet = true := by
  simp [h, step, update]

namespace Examples

def initial : World := {
  argument := 11
  closures := fun _ => none
  result := none
  nextClosure := ⟨0⟩
  setResult := none
  sets := fun _ => false
  nextSet := ⟨0⟩
  events := []
}

/-- Representative source shape: `function factory(f) { return async (...xs) =>
  wrap(await f(...xs)); } const result = factory(implementation);` crossing
  `const set = new Set();`. Calls in the arrow body are deferred. -/
theorem set_creation_accepted : commutable .pureNewSet = true := by decide +kernel

theorem later_call_observes_argument :
    (run [.invokeResult] (step .pureNewSet (make initial))).events = [.invoked (some 11)] := by decide +kernel

theorem changed_argument_rejected : commutable (.writeArgument 22) = false := by decide +kernel

theorem changed_argument_changes_call :
    (run [.invokeResult] (step (.writeArgument 22) (make initial))).events ≠
      (run [.invokeResult] (make (step (.writeArgument 22) initial))).events := by decide +kernel

theorem escape_rejected : commutable .publishResult = false := by decide +kernel

theorem escape_changes_observation :
    (step .publishResult (make initial)).events ≠ (make (step .publishResult initial)).events := by decide +kernel

theorem early_call_rejected : commutable .invokeResult = false := by decide +kernel

theorem early_call_changes_observation :
    (step .invokeResult (make initial)).events ≠ (make (step .invokeResult initial)).events := by decide +kernel

theorem reentrant_constructor_rejected : commutable .reentrantNewSet = false := by decide +kernel

theorem reentrant_constructor_changes_observation :
    (step .reentrantNewSet (make initial)).events ≠ (make (step .reentrantNewSet initial)).events := by decide +kernel

theorem other_allocation_rejected : commutable .allocateClosure = false := by decide +kernel

theorem other_allocation_changes_identity :
    (step .allocateClosure (make initial)).result ≠ (make (step .allocateClosure initial)).result := by decide +kernel

end Examples
end FreshClosure
