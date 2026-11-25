import EsmSplit.CoreJS.ClassOrigin
import EsmSplit.CoreJS.StackReadBoundary

/-!
# Reference allocation for ordinary zero-argument intrinsic Error construction

This executable reference is not used by `evalExpr`, `constructTarget`, or any checker. Inputs are already
evaluated. It covers only the modeled realm's intrinsic constructor with that same object as ordinary newTarget,
no arguments, and a prototype lookup that returns the modeled Error prototype. Other cases stay unknown.

The host continuation runs *after* allocation and can change state, throw, or be unknown. The reference does not
invent Error's host-specific own properties, stack descriptor, ErrorData behavior, stack string or capture effects.
A real integration must relate the realm object to the selected host's starting binding and implement or justify that
continuation, including ErrorData and all observable own properties and effects. In particular Node/V8 stack capture
and lazy formatting are not modeled here. `Receipt` records an allocation event, not intrinsic origin, a persistent
forge-proof brand, or an assertion that the allocated object's later properties remain unchanged. The actual
reference-run equation is required for `OriginEvidence`; an ordinary allocation alone is not sufficient.

The production integration must (1) initialize the actual selected realm's intrinsic identity and its host global
binding, preserving aliasing and later writes; (2) implement the zero-argument Error constructor in the evaluator,
including the observable newTarget prototype lookup and the `[[ErrorData]]` internal slot; (3) give the selected
host's real post-allocation transition, including the own stack descriptor, capture/formatting effects and abrupt
outcomes; and (4) relate the actual evaluator execution to an allocation witness that survives only appropriate
mutations. Node's pinned runtime has an own stack accessor and its formatting hook can throw when read. None of these
facts follows from a prototype, a name, an arbitrary callback, or this reference execution. Phase 1's `intrinsic.Error`
identity is a host-observation string with a premise, not a heap address or allocation event; its reached opaque
constructor remains unknown. Converting that observation into a starting realm binding and proving that the host
transition supplies the actual receiver and descriptors are separate obligations. Until those bridges exist,
production construction and strict admission remain unchanged.
-/

namespace CoreJS.IntrinsicErrorReference

variable {W : World}

/-- The object immediately after ECMAScript's ordinary allocation, before host finalization. ErrorData and
host-defined own properties are outside `Obj`; they must be accounted for by the integration contract. -/
def receiverAtAllocation : Obj := { proto := some errorProto, props := [] }

/-- A witnessed invocation of CoreJS `alloc`. The states pin the instant of allocation, before host code runs. -/
structure Receipt (W : World) where
  beforeAlloc : State W
  afterAlloc : State W
  receiver : Addr
  allocated : alloc receiverAtAllocation beforeAlloc = .ok receiver afterAlloc
  fresh : beforeAlloc.objAt receiver = none
  shape : afterAlloc.objAt receiver = some receiverAtAllocation

/-- Execute the real allocator and retain its witness; no caller-provided tag can replace this event. -/
def allocate : M W (Receipt W) := fun s =>
  match h : alloc receiverAtAllocation s with
  | .ok receiver t => .ok ⟨s, t, receiver, h, (alloc_ok h).1, alloc_objAt h⟩ t
  | .throw v t => .throw v t
  | .unknown why t => .unknown why t
  | .fuel => .fuel

/-- `finish` represents the selected host's entire post-allocation behavior. A production bridge must
justify that it implements the host operation, including effects and abrupt outcomes. -/
def constructZero (call : Call W) (finish : Addr → M W Unit) (callee newTarget : Val)
    (args : List Val) : M W (Receipt W) :=
  if callee = .obj errorConstructor ∧ newTarget = .obj errorConstructor ∧ args = [] then do
    let ctor ← getObj errorConstructor
    match ctor.code with
    | some (.host "%Error%") =>
      let p ← getProp call newTarget (.str "prototype")
      if p = .obj errorProto then do
        let receipt ← allocate
        finish receipt.receiver
        pure receipt
      else unknown "reference Error newTarget prototype is unsupported"
    | _ => unknown "reference realm Error constructor code is unsupported"
  else unknown "reference Error call shape is unsupported"

/-- Historical evidence is tied to the exact reference run, not to a public `Receipt`, prototype or name. -/
def OriginEvidence (call : Call W) (finish : Addr → M W Unit) (callee newTarget : Val)
    (args : List Val) (s t : State W) (receipt : Receipt W) : Prop :=
  constructZero call finish callee newTarget args s = .ok receipt t

/-- A successful reference run proves which constructor and newTarget were selected and that the
receiver really was fresh and had the specified initial shape. This is not a theorem about the production evaluator. -/
theorem successHasOrigin (call : Call W) (finish : Addr → M W Unit) (callee newTarget : Val)
    (args : List Val) (s t : State W) (receipt : Receipt W)
    (run : OriginEvidence call finish callee newTarget args s t receipt) :
    callee = .obj errorConstructor ∧ newTarget = .obj errorConstructor ∧ args = [] ∧
      receipt.beforeAlloc.objAt receipt.receiver = none ∧
      receipt.afterAlloc.objAt receipt.receiver = some receiverAtAllocation := by
  change constructZero call finish callee newTarget args s = .ok receipt t at run
  by_cases eligible : callee = .obj errorConstructor ∧ newTarget = .obj errorConstructor ∧ args = []
  · exact ⟨eligible.1, eligible.2.1, eligible.2.2, receipt.fresh, receipt.shape⟩
  · simp [constructZero, eligible, unknown] at run

/-- No caller named `Error`, lookalike object, or different newTarget gets a successful reference run
unless it has the same actual constructor identity and call shape. -/
theorem unsupportedHasNoOrigin (call : Call W) (finish : Addr → M W Unit) (callee newTarget : Val)
    (args : List Val) (s t : State W) (receipt : Receipt W)
    (different : callee ≠ .obj errorConstructor ∨ newTarget ≠ .obj errorConstructor ∨ args ≠ []) :
    constructZero call finish callee newTarget args s ≠ .ok receipt t := by
  intro run
  have origin := successHasOrigin call finish callee newTarget args s t receipt run
  rcases different with h | h | h
  · exact h origin.1
  · exact h origin.2.1
  · exact h origin.2.2.1

/-! Executable controls: these use only a deliberately empty synthetic host continuation. They do not claim
that a Node Error has no own stack property or that any real host satisfies this continuation. -/

def start (w : World) : State w := { initialState w 1 with region := 1 }
def finishEmpty (_ : Addr) : M W Unit := pure ()
def referenceCall : Call W := fun _ _ _ => unknown "unexpected call"

def aliasEnv : Env := { vars := [("Alias", ⟨0, 0⟩)], this := .undef }
def aliasStart (w : World) : State w :=
  { start w with cells := #[#[{ value := some (.obj errorConstructor), mutable := false }], #[]] }

def viaAlias : M W (Receipt W) := do
  let callee ← readVar aliasEnv "Alias"
  constructZero referenceCall finishEmpty callee callee []

def aliasAfter (w : World) : State w :=
  { aliasStart w with heap := #[builtins, #[receiverAtAllocation]] }

def aliasReceipt (w : World) : Receipt w where
  beforeAlloc := aliasStart w
  afterAlloc := aliasAfter w
  receiver := ⟨1, 0⟩
  allocated := rfl
  fresh := rfl
  shape := rfl

/-- The alias resolves to the actual intrinsic and produces a witnessed fresh allocation in the synthetic host. -/
theorem aliasAllocation :
    viaAlias (aliasStart W) = .ok (aliasReceipt W) (aliasAfter W) ∧
      OriginEvidence referenceCall finishEmpty (.obj errorConstructor) (.obj errorConstructor) []
        (aliasStart W) (aliasAfter W) (aliasReceipt W) := by
  exact ⟨rfl, rfl⟩

def successControl : Res World.probe (Receipt World.probe) → Bool
  | .ok receipt _ => receipt.receiver == ⟨1, 0⟩ &&
      (receipt.beforeAlloc.objAt receipt.receiver).isNone &&
      (receipt.afterAlloc.objAt receipt.receiver).any (fun o => o.proto == some errorProto && o.props.isEmpty)
  | _ => false

def unknownControl {α : Type} : Res World.probe α → Bool
  | .unknown .. => true
  | _ => false

#guard successControl (constructZero referenceCall finishEmpty (.obj errorConstructor) (.obj errorConstructor) []
  (start World.probe))
#guard successControl (viaAlias (aliasStart World.probe))
/-- Ordinary objects and user constructors may share names and prototypes with Error. -/
def lookalike : Obj := { proto := some errorProto, props := [] }
def shadowConstructor : Obj :=
  { proto := some functionProto, code := some (.host "user Error"), props := [
    ("name", .data (.str "Error") false false true),
    ("prototype", .data (.obj errorProto) true false false)] }
def controlStart (w : World) : State w :=
  { start w with heap := #[builtins, #[lookalike, shadowConstructor]], cells := #[#[{ value := some (.obj ⟨1, 1⟩), mutable := false }], #[]] }
def shadowEnv : Env := { vars := [("Error", ⟨0, 0⟩)], this := .undef }
def viaShadow : M W (Receipt W) := do
  let callee ← readVar shadowEnv "Error"
  constructZero referenceCall finishEmpty callee callee []

#guard match alloc lookalike (start World.probe) with
  | .ok a t => a == ⟨1, 0⟩ && (t.objAt a).any (fun o => o.proto == some errorProto) &&
      unknownControl (constructZero referenceCall finishEmpty (.obj a) (.obj a) [] t)
  | _ => false
#guard match readVar shadowEnv "Error" (controlStart World.probe) with
  | .ok (.obj a) t => a == ⟨1, 1⟩ &&
      (t.objAt a).any (fun o => (o.props.lookup (.str "name")).isSome && o.proto == some functionProto) &&
      unknownControl (viaShadow t)
  | _ => false
/-- A same-named shadow remains unknown; it cannot produce an intrinsic-origin receipt. -/
theorem shadowRejected :
    viaShadow (controlStart W) = .unknown "reference Error call shape is unsupported" (controlStart W) := by
  rfl

/-- The address of the ordinary Error.prototype lookalike does not select the intrinsic constructor. -/
theorem lookalikeRejected (s : State W) :
    constructZero referenceCall finishEmpty (.obj ⟨1, 0⟩) (.obj ⟨1, 0⟩) [] s =
      .unknown "reference Error call shape is unsupported" s := by
  rfl

/-- An alternate newTarget cannot obtain ordinary-newTarget evidence, even for the intrinsic callee. -/
theorem alternateTargetRejected (s : State W) :
    constructZero referenceCall finishEmpty (.obj errorConstructor) (.obj ⟨1, 1⟩) [] s =
      .unknown "reference Error call shape is unsupported" s := by
  rfl

-- A different newTarget is outside this ordinary-construction slice, even for the intrinsic callee.
#guard unknownControl (constructZero referenceCall finishEmpty (.obj errorConstructor) (.obj ⟨1, 1⟩) []
  (controlStart World.probe))

/-- Synthetic controls: a host throw after allocation must retain the allocated state. -/
def finishThrows (_ : Addr) : M W Unit := throwVal (.str "host threw")
#guard match constructZero referenceCall finishThrows (.obj errorConstructor) (.obj errorConstructor) []
    (start World.probe) with
  | .throw (.str why) t => why == "host threw" && (t.objAt ⟨1, 0⟩).any (fun o => o.proto == some errorProto)
  | _ => false

def afterReferenceAllocation (w : World) : State w :=
  { start w with heap := #[builtins, #[receiverAtAllocation]] }

/-- A host finalization throw is not swallowed and retains the freshly allocated receiver. -/
theorem hostThrowRetained :
    constructZero referenceCall finishThrows (.obj errorConstructor) (.obj errorConstructor) [] (start W) =
      .throw (.str "host threw") (afterReferenceAllocation W) := by
  rfl

/-- Even an abnormal synthetic descriptor runs its getter, including a throw, before allocating. -/
def getterState (w : World) : State w :=
  { start w with heap := (start w).heap.modify errorConstructor.region (·.modify errorConstructor.index
      fun o => { o with props := [("prototype", .accessor (.host "getter") .undef false false)] }) }
def throwingCall : Call W := fun _ _ _ => throwVal (.str "getter threw")
#guard match constructZero throwingCall finishEmpty (.obj errorConstructor) (.obj errorConstructor) []
    (getterState World.probe) with
  | .throw (.str why) t => why == "getter threw" && (t.objAt ⟨1, 0⟩).isNone
  | _ => false

/-- Prototype lookup exceptions occur before allocation and are retained. -/
theorem prototypeThrowRetained :
    constructZero throwingCall finishEmpty (.obj errorConstructor) (.obj errorConstructor) [] (getterState W) =
      .throw (.str "getter threw") (getterState W) := by
  rfl

/-- Production remains unknown even for the real modeled intrinsic. -/
theorem productionStillUnknown (fuel : Nat) (s : State W)
    (constructor : s.objAt errorConstructor = some errorConstructorObject) :
    constructTarget (fuel + 1) (.obj errorConstructor) (.obj errorConstructor) [] s =
      .unknown "host function %Error% is constructed" s :=
  StackReadBoundary.errorConstructionUnknown fuel s constructor

#print axioms successHasOrigin
#print axioms unsupportedHasNoOrigin
#print axioms aliasAllocation
#print axioms shadowRejected
#print axioms lookalikeRejected
#print axioms alternateTargetRejected
#print axioms hostThrowRetained
#print axioms prototypeThrowRetained
#print axioms productionStillUnknown

end CoreJS.IntrinsicErrorReference
