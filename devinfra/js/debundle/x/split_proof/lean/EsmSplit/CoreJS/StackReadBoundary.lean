import EsmSplit.CoreJS.Eval

/-!
# A reached stack-property read

This is a conditional CoreJS lemma, not a way to recognize a JavaScript Error
or a public certificate. A host/profile must separately justify the getter's
identity and behavior at the reached receiver and state. The descriptor is
looked up using CoreJS's real prototype search; the string `stack` alone does
not establish that the getter is built in. Only CoreJS-representable strings
are covered, not JavaScript strings with isolated UTF-16 surrogates.
-/

namespace CoreJS.StackReadBoundary

variable {W : World}

private theorem stringKey (s : String) : (toKey (.str s) : M W PropertyKey) = pure (.str s) := rfl

/-- At one reached state, the first descriptor in the receiver's prototype
chain must actually be the independently identified getter. The profile
assumption concerns its call on this receiver at this state only; other objects,
properties and states may have arbitrary overrides. -/
theorem getPropAt (call : Call W) (genuine : Val → Prop) (receiver : Addr)
    (state : State W) (getter setter : Val) (enumerable configurable : Bool) (stack : String)
    (identity : genuine getter) (defined : getter ≠ .undef)
    (descriptor : findProp state state.objectCount receiver (.str "stack") =
      some (.accessor getter setter enumerable configurable))
    (profile : genuine getter → call getter (.obj receiver) [] state = .ok (.str stack) state) :
    getProp call (.obj receiver) (.str "stack") state = .ok (.str stack) state := by
  have returns := profile identity
  cases getter <;>
    simp_all [getProp, getFrom, stringKey, lookupProp, getState, bind, pure]

/-- Both reads are checked at this one state, not assumed for every state. -/
theorem twoReadsAt (call : Call W) (genuine : Val → Prop) (receiver : Addr)
    (state : State W) (getter setter : Val) (enumerable configurable : Bool) (stack : String)
    (identity : genuine getter) (defined : getter ≠ .undef)
    (descriptor : findProp state state.objectCount receiver (.str "stack") =
      some (.accessor getter setter enumerable configurable))
    (profile : genuine getter → call getter (.obj receiver) [] state = .ok (.str stack) state) :
    (do let first ← getProp call (.obj receiver) (.str "stack")
        let second ← getProp call (.obj receiver) (.str "stack")
        pure (first, second)) state = .ok (.str stack, .str stack) state := by
  have read := getPropAt call genuine receiver state getter setter enumerable configurable stack
    identity defined descriptor profile
  simp [bind, pure, read]

/-- This is the existing evaluator's property-read case. The receiver has
already run, possibly changing state or making requests; the proof retains its
actual reached state. Its genuine-getter evidence and profile behavior have to
hold at that state. -/
theorem evalReadAt (fuel : Nat) (env : Env) (receiverExpr : Expr) (start reached : State W)
    (receiver : Addr) (genuine : Val → Prop) (getter setter : Val) (enumerable configurable : Bool) (stack : String)
    (receiverRun : evalExpr (fuel + 1) env receiverExpr start = .ok (.obj receiver) reached)
    (identity : genuine getter) (defined : getter ≠ .undef)
    (descriptor : findProp reached reached.objectCount receiver (.str "stack") =
      some (.accessor getter setter enumerable configurable))
    (profile : genuine getter → callFn (fuel + 1) getter (.obj receiver) [] reached = .ok (.str stack) reached) :
    evalExpr (fuel + 2) env (.get receiverExpr (.str "stack")) start = .ok (.str stack) reached := by
  have read := getPropAt (callFn (fuel + 1)) genuine receiver reached getter setter enumerable configurable stack
    identity defined descriptor profile
  change (do let base ← evalExpr (fuel + 1) env receiverExpr
             getProp (callFn (fuel + 1)) base (.str "stack")) start = _
  simp [bind, receiverRun, read]

/-- A receiver/constructor throw is propagated with its reached state; no
property access or shared-string substitution is performed afterward. -/
theorem receiverThrow (fuel : Nat) (env : Env) (receiverExpr : Expr) (start reached : State W) (error : Val)
    (receiverRun : evalExpr (fuel + 1) env receiverExpr start = .throw error reached) :
    evalExpr (fuel + 2) env (.get receiverExpr (.str "stack")) start = .throw error reached := by
  change (do let base ← evalExpr (fuel + 1) env receiverExpr
             getProp (callFn (fuel + 1)) base (.str "stack")) start = _
  simp [bind, receiverRun]

/-! The following synthetic heap illustrates the boundary. The designation of
its getter as `genuine` and the call behavior are explicit example premises,
not a claim that CoreJS recognizes a real Error stack getter. -/

def receiver : Addr := ⟨0, 0⟩
def getter : Addr := ⟨0, 1⟩
def other : Addr := ⟨0, 2⟩
def marker : Addr := ⟨0, 0⟩
def receiverCell : Addr := ⟨0, 1⟩

def builtinObject : Obj :=
  { proto := none, props := [(.str "stack", .accessor (.obj getter) .undef true true)] }

def overrideObject : Obj :=
  { proto := none, props := [(.str "stack", .accessor (.host "user getter") .undef true true)] }

def start (w : World) : State w where
  heap := #[#[builtinObject, { proto := none, props := [] }, overrideObject]]
  cells := #[#[{ value := some (.bool false), mutable := true },
    { value := some (.obj receiver), mutable := false }]]
  region := 0
  out := #[]
  world := { state := w.init, handed := #[] }

def reached (w : World) : State w :=
  { start w with cells := #[#[{ value := some (.bool true), mutable := true },
    { value := some (.obj receiver), mutable := false }]] }

def sampleCall (stack : String) : Call W := fun callee this args =>
  if callee = .obj getter ∧ this = .obj receiver ∧ args = [] then pure (.str stack) else do
    setCell marker (.bool false)
    throwVal (.str "user getter threw")

def sampleEnv : Env :=
  { vars := [("flag", marker), ("instance", receiverCell)], this := .undef }

def sampleReceiverExpr : Expr :=
  .seq (.assign (.var "flag") (.bool true)) (.var "instance")

/-- A real CoreJS receiver expression performs the write before yielding the
receiver. The later property-read theorem receives this exact reached state. -/
theorem evalReceiverEffect :
    evalExpr 5 sampleEnv sampleReceiverExpr (start W) = .ok (.obj receiver) (reached W) := by
  rfl

/-- The receiver expression writes the marker before the property read. The
other object has an unrelated overriding getter and is still allowed. The
example call models the assumed getter; it is not a real Error builtin. -/
theorem sampleReached (stack : String) :
    evalExpr 5 sampleEnv sampleReceiverExpr (start W) = .ok (.obj receiver) (reached W) ∧
    (do let base ← evalExpr 5 sampleEnv sampleReceiverExpr
        getProp (sampleCall stack) base (.str "stack")) (start W) = .ok (.str stack) (reached W) ∧
    (reached W).cellAt marker = some { value := some (.bool true), mutable := true } := by
  have before := evalReceiverEffect (W := W)
  have descriptor : findProp (reached W) (reached W).objectCount receiver (.str "stack") =
      some (.accessor (.obj getter) .undef true true) := rfl
  have read := getPropAt (sampleCall stack) (fun v => v = .obj getter) receiver (reached W)
    (.obj getter) .undef true true stack rfl (by simp) descriptor (by intro _; rfl)
  refine ⟨before, ?_, rfl⟩
  simpa [bind, before] using read

/-- Shadow the first receiver's actual `stack` descriptor with a user getter. -/
def shadowed (w : World) : State w :=
  { reached w with heap := #[#[overrideObject, { proto := none, props := [] }, overrideObject]] }

def shadowAfter (w : World) : State w :=
  { shadowed w with cells := #[#[{ value := some (.bool false), mutable := true },
    { value := some (.obj receiver), mutable := false }]] }

/-- The shadowing getter fails the builtin-descriptor check and its state update
and throw are retained. -/
theorem overrideRetained (stack : String) :
    findProp (shadowed W) (shadowed W).objectCount receiver (.str "stack") ≠
      some (.accessor (.obj getter) .undef true true) ∧
    getProp (sampleCall stack) (.obj receiver) (.str "stack") (shadowed W) =
      .throw (.str "user getter threw") (shadowAfter W) := by
  constructor
  · simp [shadowed, reached, start, overrideObject, receiver, getter, State.objectCount, findProp, State.objAt]
  · rfl

/-- The modeled intrinsic Error constructor is still opaque. A checked bridge
from a real construction to a receiver with the stack descriptor is missing. -/
theorem errorConstructionUnknown (fuel : Nat) (state : State W)
    (constructor : state.objAt errorConstructor = some errorConstructorObject) :
    constructTarget (fuel + 1) (.obj errorConstructor) (.obj errorConstructor) [] state =
      .unknown "host function %Error% is constructed" state := by
  simp [constructTarget, getObj, constructor, errorConstructorObject, bind, unknown, toString]

/-- Engine-created error values also do not expose their properties in the
current model. -/
theorem engineErrorReadUnknown (call : Call W) (state : State W) (message : String) :
    getProp call (.error .type message) (.str "stack") state =
      .unknown "a property of an engine error" state := rfl

#print axioms getPropAt
#print axioms twoReadsAt
#print axioms evalReadAt
#print axioms receiverThrow
#print axioms evalReceiverEffect
#print axioms sampleReached
#print axioms overrideRetained
#print axioms errorConstructionUnknown
#print axioms engineErrorReadUnknown

end CoreJS.StackReadBoundary
