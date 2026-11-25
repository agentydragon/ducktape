import EsmSplit.CoreJS.SharedStackString
import EsmSplit.CoreJS.StackReadBoundary

/-!
# A registry write with reached-state stack evidence

The CoreJS heap supplies the actual receiver and descriptor checks. The host
premises below identify the getter and describe only the four calls reached in
this example. A caller-supplied predicate does not establish that this receiver
was allocated by the realm's `%Error%` intrinsic, that a getter is not borrowed,
or that an overridden read is safe. Real V1 eligibility needs those separately
checked facts. This is not a public certificate. Strings are restricted to
CoreJS's representable `String` domain.
-/

namespace CoreJS.StackReadComposition

variable {W : World}

open SharedStackString

def receiver : Addr := ⟨0, 1⟩
def getter : Addr := ⟨0, 2⟩

def receiverObject : Obj :=
  { proto := none, props := [(.str "stack", .accessor (.obj getter) .undef true true)] }

def emptyObject : Obj := { proto := none, props := [] }

/-- The existing null-prototype registry, a separate receiver/getter, and an
unrelated object with an overriding getter. -/
def initial (w : World) : State w :=
  { SharedStackString.initial w with
    heap := #[#[emptyObject, receiverObject, emptyObject, StackReadBoundary.overrideObject]] }

def marked (w : World) : State w :=
  { initial w with cells := #[#[{ value := some (.bool true), mutable := true }]] }

def written (w : World) (stack : String) (value : Val) : State w :=
  { marked w with
    heap := #[#[{ proto := none, props := [(.str stack, .data value true true true)] },
      receiverObject, emptyObject, StackReadBoundary.overrideObject]] }

def writtenBeforeMark (w : World) (stack : String) (value : Val) : State w :=
  { written w stack value with cells := #[#[{ value := some (.bool false), mutable := true }]] }

/-- A read is the existing CoreJS operation, not a substituted result. -/
def siteRead (calls : Nat → Call W) (site : Nat) : M W Val :=
  getProp (calls site) (.obj receiver) (.str "stack")

/-- Host/profile facts: the getter identity is genuine, and the two calls at
these particular sites, receiver and reached state return the shared string
without effects. This says nothing about unrelated objects or other states.
The predicate is a supplied assumption, not evidence of Error allocation. -/
def HostPair (genuine : Val → Prop) (calls : Nat → Call W) (state : State W)
    (first second : Nat) (stack : String) : Prop :=
  genuine (.obj getter) ∧
  calls first (.obj getter) (.obj receiver) [] state = .ok (.str stack) state ∧
  calls second (.obj getter) (.obj receiver) [] state = .ok (.str stack) state

private theorem stringKey (s : String) : (toKey (.str s) : M W PropertyKey) = pure (.str s) := rfl

/-- Reuse the registry example's `write`, reducing its two reads only at the
state where that write actually runs. -/
private theorem writeAt (read : Nat → M W Val) (first second : Nat) (stack : String)
    (value : Val) (state : State W)
    (firstRead : read first state = .ok (.str stack) state)
    (secondRead : read second state = .ok (.str stack) state) :
    SharedStackString.write read first second value state =
      setProp noCall (.obj registry) (.str stack) value state := by
  simp [SharedStackString.write, bind, firstRead, secondRead]

/-- The original and moved computations from `SharedStackString` compare equal
using only the four reached getter calls. The mutant really writes `2` and is
distinguished by the modeled registry lookup and complete final state. -/
theorem movedWithReachedReads (stack : String) (genuine : Val → Prop)
    (left right : Nat → Call W)
    (leftHost : HostPair genuine left (marked W) 0 1 stack)
    (rightHost : HostPair genuine right (initial W) 2 3 stack) :
    original (siteRead left) (initial W) = .ok () (written W stack (.num 1)) ∧
    moved (siteRead right) (.num 1) (initial W) = .ok () (written W stack (.num 1)) ∧
    moved (siteRead right) (.num 2) (initial W) = .ok () (written W stack (.num 2)) ∧
    getProp noCall (.obj registry) (.str stack) (written W stack (.num 1)) =
      .ok (.num 1) (written W stack (.num 1)) ∧
    getProp noCall (.obj registry) (.str stack) (written W stack (.num 2)) =
      .ok (.num 2) (written W stack (.num 2)) ∧
    written W stack (.num 1) ≠ written W stack (.num 2) := by
  have markedDescriptor : findProp (marked W) (marked W).objectCount receiver (.str "stack") =
      some (.accessor (.obj getter) .undef true true) := rfl
  have initialDescriptor : findProp (initial W) (initial W).objectCount receiver (.str "stack") =
      some (.accessor (.obj getter) .undef true true) := rfl
  have l0 : siteRead left 0 (marked W) = .ok (.str stack) (marked W) :=
    StackReadBoundary.getPropAt (left 0) genuine receiver (marked W) (.obj getter) .undef true true stack
      leftHost.1 (by simp) markedDescriptor (by intro _; exact leftHost.2.1)
  have l1 : siteRead left 1 (marked W) = .ok (.str stack) (marked W) :=
    StackReadBoundary.getPropAt (left 1) genuine receiver (marked W) (.obj getter) .undef true true stack
      leftHost.1 (by simp) markedDescriptor (by intro _; exact leftHost.2.2)
  have r2 : siteRead right 2 (initial W) = .ok (.str stack) (initial W) :=
    StackReadBoundary.getPropAt (right 2) genuine receiver (initial W) (.obj getter) .undef true true stack
      rightHost.1 (by simp) initialDescriptor (by intro _; exact rightHost.2.1)
  have r3 : siteRead right 3 (initial W) = .ok (.str stack) (initial W) :=
    StackReadBoundary.getPropAt (right 3) genuine receiver (initial W) (.obj getter) .undef true true stack
      rightHost.1 (by simp) initialDescriptor (by intro _; exact rightHost.2.2)
  have sourceWrite := writeAt (siteRead left) 0 1 stack (.num 1) (marked W) l0 l1
  have splitWrite (value : Val) := writeAt (siteRead right) 2 3 stack value (initial W) r2 r3
  have mark : setCell marker (.bool true) (initial W) = .ok () (marked W) := rfl
  have putMarked (value : Val) : setProp noCall (.obj registry) (.str stack) value (marked W) =
      .ok () (written W stack value) := by
    simp [setProp, stringKey, registry, lookupProp, getState, State.objectCount, findProp,
      State.objAt, getObj, defineOwn, applyDesc, modifyObj, setOwn, initial, marked, written,
      SharedStackString.initial, emptyObject, receiverObject, StackReadBoundary.overrideObject,
      bind, pure, lift, Except.pure]
  have putInitial (value : Val) : setProp noCall (.obj registry) (.str stack) value (initial W) =
      .ok () (writtenBeforeMark W stack value) := by
    simp [setProp, stringKey, registry, lookupProp, getState, State.objectCount, findProp,
      State.objAt, getObj, defineOwn, applyDesc, modifyObj, setOwn, initial, marked, written,
      writtenBeforeMark, SharedStackString.initial, emptyObject, receiverObject,
      StackReadBoundary.overrideObject, bind, pure, lift, Except.pure]
  have markWritten (value : Val) : setCell marker (.bool true) (writtenBeforeMark W stack value) =
      .ok () (written W stack value) := rfl
  have originalRun : original (siteRead left) (initial W) = .ok () (written W stack (.num 1)) := by
    simp [original, bind, mark, sourceWrite, putMarked]
  have movedRun (value : Val) : moved (siteRead right) value (initial W) = .ok () (written W stack value) := by
    simp [moved, bind, splitWrite, putInitial, markWritten]
  have lookupOne : getProp noCall (.obj registry) (.str stack) (written W stack (.num 1)) =
      .ok (.num 1) (written W stack (.num 1)) := by
    simp [getProp, getFrom, stringKey, lookupProp, getState, State.objectCount, findProp,
      State.objAt, registry, bind, pure, written, marked, initial, SharedStackString.initial,
      emptyObject, receiverObject, StackReadBoundary.overrideObject]
  have lookupTwo : getProp noCall (.obj registry) (.str stack) (written W stack (.num 2)) =
      .ok (.num 2) (written W stack (.num 2)) := by
    simp [getProp, getFrom, stringKey, lookupProp, getState, State.objectCount, findProp,
      State.objAt, registry, bind, pure, written, marked, initial, SharedStackString.initial,
      emptyObject, receiverObject, StackReadBoundary.overrideObject]
  refine ⟨originalRun, movedRun (.num 1), movedRun (.num 2), lookupOne, lookupTwo, ?_⟩
  intro same
  have sameRead := congrArg (getProp noCall (.obj registry) (.str stack)) same
  rw [lookupOne, lookupTwo] at sameRead
  exact (SharedStackString.mutantLookup (W := W) stack).2.2 (Res.ok.inj sameRead).1

/-- A synthetic profile witnesses that the local assumptions can hold even
when an unrelated object has an overriding getter. Its identity check is an
example only; it does not identify a genuine JavaScript Error getter. -/
def exampleCalls (stack : String) (_ : Nat) : Call W := fun callee this args =>
  if callee = .obj getter ∧ this = .obj receiver ∧ args = [] then pure (.str stack)
  else throwVal (.str "unrelated getter threw")

/-- The example executes the original and moved writes, while reading the
unrelated getter still throws rather than being replaced by the shared string. -/
theorem exampleWithOverride (stack : String) :
    original (siteRead (exampleCalls stack)) (initial W) = .ok () (written W stack (.num 1)) ∧
    moved (siteRead (exampleCalls stack)) (.num 1) (initial W) = .ok () (written W stack (.num 1)) ∧
    getProp (exampleCalls stack 4) (.obj ⟨0, 3⟩) (.str "stack") (initial W) =
      .throw (.str "unrelated getter threw") (initial W) := by
  let genuine : Val → Prop := fun v => v = .obj getter
  have leftHost : HostPair genuine (exampleCalls stack) (marked W) 0 1 stack := ⟨rfl, rfl, rfl⟩
  have rightHost : HostPair genuine (exampleCalls stack) (initial W) 2 3 stack := ⟨rfl, rfl, rfl⟩
  have compared := movedWithReachedReads stack genuine (exampleCalls stack) (exampleCalls stack) leftHost rightHost
  exact ⟨compared.1, compared.2.1, rfl⟩

/-- A synthetic caller predicate can satisfy the local read premise for an
ordinary receiver. It is not evidence of intrinsic Error allocation, a real
builtin getter, or eligibility for the V1 substitution. -/
theorem syntheticIdentityOnly (stack : String) :
    HostPair (fun v => v = .obj getter) (exampleCalls stack) (initial W) 2 3 stack ∧
    receiverObject.proto = none ∧
    getProp (exampleCalls stack 2) (.obj receiver) (.str "stack") (initial W) =
      .ok (.str stack) (initial W) := by
  exact ⟨⟨rfl, rfl, rfl⟩, rfl, rfl⟩

/-! These controls exercise the existing realm identity and object representation.
They do not certify any real Error construction or stack substitution. -/

def shadowConstructor : Addr := ⟨1, 0⟩
def lookalike : Addr := ⟨1, 1⟩

def shadowObject : Obj :=
  { proto := some functionProto, code := some (.host "user Error"),
    props := [(.str "name", .data (.str "Error") false false true)] }

def lookalikeObject : Obj := { proto := some errorProto, props := [] }

def identityState (w : World) : State w :=
  { initialState w 1 with
    heap := #[builtins, #[shadowObject, lookalikeObject]],
    cells := #[#[{ value := some (.obj errorConstructor), mutable := false },
      { value := some (.obj shadowConstructor), mutable := false }]] }

def identityEnv : Env := { vars := [("Alias", ⟨0, 0⟩), ("Error", ⟨0, 1⟩)], this := .undef }

/-- The alias has the actual realm constructor identity, while a shadowing
binding named `Error` is a different constructor. A separate ordinary object
can have Error.prototype without acquiring allocation provenance. -/
theorem aliasShadowAndLookalike :
    evalExpr 1 identityEnv (.var "Alias") (identityState W) =
      .ok (.obj errorConstructor) (identityState W) ∧
    evalExpr 1 identityEnv (.var "Error") (identityState W) =
      .ok (.obj shadowConstructor) (identityState W) ∧
    getProp noCall (.obj shadowConstructor) (.str "name") (identityState W) =
      .ok (.str "Error") (identityState W) ∧
    (identityState W).objAt lookalike = some lookalikeObject ∧
    lookalikeObject.proto = some errorProto ∧
    (Val.obj shadowConstructor : Val) ≠ .obj errorConstructor := by
  refine ⟨rfl, rfl, rfl, rfl, rfl, ?_⟩
  intro equal
  have addresses := Val.obj.inj equal
  have regions := congrArg Addr.region addresses
  simp [shadowConstructor, errorConstructor, realmSlotAddr] at regions

def ordinaryBase (w : World) : State w := { initialState w 1 with region := 1 }

/-- An ordinary allocation can have `Error.prototype` without invoking the
intrinsic constructor. Prototype shape therefore cannot prove V1 provenance. -/
theorem ordinaryPrototypeLookalike :
    alloc lookalikeObject (ordinaryBase W) =
      .ok ⟨1, 0⟩ { ordinaryBase W with heap := #[builtins, #[lookalikeObject]] } ∧
    lookalikeObject.proto = some errorProto := by
  exact ⟨rfl, rfl⟩

/-- Even the alias of the modeled realm intrinsic cannot yet produce an
Error instance: construction is unknown, so there is no V1 provenance bridge. -/
theorem aliasConstructionUnknown (fuel : Nat) :
    evalExpr (fuel + 2) identityEnv (.new (.var "Alias") []) (identityState W) =
      .unknown "host function %Error% is constructed" (identityState W) := by
  have constructor : (identityState W).objAt errorConstructor = some errorConstructorObject := by rfl
  have unknown := StackReadBoundary.errorConstructionUnknown fuel (identityState W) constructor
  change (do let callee ← readVar identityEnv "Alias"
             constructTarget (fuel + 1) callee callee []) (identityState W) = _
  simpa [bind, readVar, identityEnv, identityState, getState, State.cellAt, pure] using unknown

/-- A same-named shadow constructor is not the intrinsic. Its unmodeled host
body also remains unknown when actually constructed. -/
theorem shadowConstructionUnknown (fuel : Nat) :
    constructTarget (fuel + 1) (.obj shadowConstructor) (.obj shadowConstructor) [] (identityState W) =
      .unknown "host function user Error is constructed" (identityState W) := by
  rfl

#print axioms movedWithReachedReads
#print axioms exampleWithOverride
#print axioms syntheticIdentityOnly
#print axioms aliasShadowAndLookalike
#print axioms ordinaryPrototypeLookalike
#print axioms aliasConstructionUnknown
#print axioms shadowConstructionUnknown

end CoreJS.StackReadComposition
