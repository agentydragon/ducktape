import EsmSplit.CoreJS.Value

/-!
# A conditional shared-stack-string comparison

This is proof groundwork, not a checker or an Error implementation. The
assumption below starts at a read already justified to be the supported built-in
stack observation. Error construction, receiver evaluation and identification of
the actual built-in getter are not modeled by this assumption. In particular,
spelling a property `stack` is insufficient to establish it.

`String` is CoreJS's representable string domain; a JavaScript string containing
an isolated UTF-16 surrogate is outside this model and needs a separate bridge.
-/

namespace CoreJS.SharedStackString

variable {W : World}

/-- Every justified built-in read, at any site and state in either execution,
returns the same string and has no other effect. The quantification over states
includes repeated reads; it does not justify replacing an arbitrary getter. -/
def SharedRead (read : Nat → M W Val) (stack : String) : Prop :=
  ∀ site state, read site state = .ok (.str stack) state

def registry : Addr := ⟨0, 0⟩
def marker : Addr := ⟨0, 0⟩

/-- An ordinary null-prototype registry makes every string, including
`__proto__`, a plain own data key. The cell is independent of the registry. -/
def initial (W : World) : State W where
  heap := #[#[{ proto := none, props := [] }]]
  cells := #[#[{ value := some (.bool false), mutable := true }]]
  region := 0
  out := #[]
  world := { state := W.init, handed := #[] }

def final (W : World) (key : String) (v : Val) : State W where
  heap := #[#[{ proto := none, props := [(.str key, .data v true true true)] }]]
  cells := #[#[{ value := some (.bool true), mutable := true }]]
  region := 0
  out := #[]
  world := { state := W.init, handed := #[] }

def noCall : Call W := fun _ _ _ => unknown "unexpected accessor"

private theorem stringKey (s : String) : (toKey (.str s) : M W PropertyKey) = pure (.str s) := rfl

/-- Two reads at independently chosen sites are compared before the actual
CoreJS strict property write. -/
def write (read : Nat → M W Val) (first second : Nat) (v : Val) : M W Unit := do
  let a ← read first
  let b ← read second
  if a = b then setProp noCall (.obj registry) b v
  else throwVal (.str "different stack reads")

def original (read : Nat → M W Val) : M W Unit := do
  setCell marker (.bool true)
  write read 0 1 (.num 1)

def moved (read : Nat → M W Val) (value : Val) : M W Unit := do
  write read 2 3 value
  setCell marker (.bool true)

/-- The distinct sites and the repeated read may have different implementations
on the two sides; the explicit common-string premise suffices. The moved write
and the independent cell effect produce the same complete CoreJS state. -/
theorem movedWrite (stack : String) (left right : Nat → M W Val)
    (hl : SharedRead left stack) (hr : SharedRead right stack) :
    original left (initial W) = .ok () (final W stack (.num 1)) ∧
    moved right (.num 1) (initial W) = .ok () (final W stack (.num 1)) ∧
    moved right (.num 2) (initial W) = .ok () (final W stack (.num 2)) := by
  have leftEq : left = fun _ => pure (.str stack) := by
    funext site state
    exact hl site state
  have rightEq : right = fun _ => pure (.str stack) := by
    funext site state
    exact hr site state
  rw [leftEq, rightEq]
  simp [original, moved, write, bind, pure, setCell, marker, setProp, stringKey,
    registry, lift, lookupProp, getState, State.objectCount, findProp,
    State.objAt, getObj, defineOwn, applyDesc, modifyObj, setOwn, initial, final, Except.pure]

/-- Reading back the written own data property exposes the changed value. -/
theorem mutantLookup (stack : String) :
    getProp noCall (.obj registry) (.str stack) (final W stack (.num 1)) =
      .ok (.num 1) (final W stack (.num 1)) ∧
    getProp noCall (.obj registry) (.str stack) (final W stack (.num 2)) =
      .ok (.num 2) (final W stack (.num 2)) ∧
    (Val.num 1 : Val) ≠ .num 2 := by
  refine ⟨?_, ?_, ?_⟩
  · simp [getProp, getFrom, stringKey, lookupProp, getState, State.objectCount,
      findProp, State.objAt, registry, bind, pure, final]
  · simp [getProp, getFrom, stringKey, lookupProp, getState, State.objectCount,
      findProp, State.objAt, registry, bind, pure, final]
  · decide

/-- The changed write leads to a different final state, not merely a different
proof witness. Reading its shared key distinguishes the values. -/
theorem mutantDistinguished (stack : String) (left right : Nat → M W Val)
    (hl : SharedRead left stack) (hr : SharedRead right stack) :
    original left (initial W) = .ok () (final W stack (.num 1)) ∧
    moved right (.num 2) (initial W) = .ok () (final W stack (.num 2)) ∧
    final W stack (.num 1) ≠ final W stack (.num 2) := by
  obtain ⟨originalRun, _, mutantRun⟩ := movedWrite stack left right hl hr
  refine ⟨originalRun, mutantRun, ?_⟩
  intro same
  have observations := mutantLookup (W := W) stack
  have eq := congrArg (getProp noCall (.obj registry) (.str stack)) same
  rw [observations.1, observations.2.1] at eq
  exact observations.2.2 (Res.ok.inj eq).1

/-- A concrete dangerous-looking key is still plain data on the null-prototype
registry; no prototype setter or inherited accessor is invoked. -/
theorem protoKeyExample :
    original (fun _ => pure (.str "__proto__")) (initial World.probe) =
      .ok () (final World.probe "__proto__" (.num 1)) ∧
    moved (fun _ => pure (.str "__proto__")) (.num 1) (initial World.probe) =
      .ok () (final World.probe "__proto__" (.num 1)) := by
  obtain ⟨originalRun, movedRun, _⟩ :=
    movedWrite (W := World.probe) "__proto__" (fun _ => pure (.str "__proto__"))
      (fun _ => pure (.str "__proto__")) (by intro _ _; rfl) (by intro _ _; rfl)
  exact ⟨originalRun, movedRun⟩

def getterState (W : World) : State W where
  heap := #[#[{ proto := none, props := [(.str "stack", .accessor (.host "getter") .undef true true)] }]]
  cells := #[]
  region := 0
  out := #[]
  world := { state := W.init, handed := #[] }

def throwingGetter : Call W := fun _ _ _ => throwVal (.str "getter threw")
def overrideRead (_ : Nat) : M W Val :=
  getProp throwingGetter (.obj registry) (.str "stack")

/-- A user getter named `stack` really throws. Its spelling does not supply the
shared built-in-read premise, so its effect cannot be replaced with a string. -/
theorem overrideThrows (stack : String) :
    overrideRead 0 (getterState W) = .throw (.str "getter threw") (getterState W) ∧
    ¬ SharedRead (overrideRead (W := W)) stack := by
  constructor
  · rfl
  · intro shared
    have impossible := shared 0 (getterState W)
    have thrown : overrideRead 0 (getterState W) = .throw (.str "getter threw") (getterState W) := rfl
    rw [thrown] at impossible
    cases impossible

/-- CoreJS explicitly refuses a host string containing an isolated UTF-16
surrogate. Extending this proof to all JavaScript strings requires representing
such strings and property keys before any public certificate uses the contract. -/
theorem isolatedSurrogateRefused (state : State W) :
    fromW .illFormedString state =
      .unknown "the world gives a string containing an isolated UTF-16 surrogate" state := rfl

#print axioms movedWrite
#print axioms mutantLookup
#print axioms mutantDistinguished
#print axioms protoKeyExample
#print axioms overrideThrows
#print axioms isolatedSurrogateRefused

end CoreJS.SharedStackString
