import EsmSplit.CoreJS.SharedStackString
import EsmSplit.CoreJS.StackReadBoundary

/-!
# A registry write with reached-state stack evidence

The CoreJS heap supplies the actual receiver and descriptor checks. The host
premises below identify the genuine getter and describe only the four calls
reached in this example. This does not identify a real Error getter or prove
its construction; it is not a public certificate. Strings are restricted to
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
without effects. This says nothing about unrelated objects or other states. -/
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

#print axioms movedWithReachedReads
#print axioms exampleWithOverride

end CoreJS.StackReadComposition
