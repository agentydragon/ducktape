import EsmSplit.CoreJS.DataProjection
import EsmSplit.CoreJS.NativeRecord

/-! Evaluator-side obligation for a loader that represents a module's metadata
by a hidden cell. The current linker does not produce this cell. -/

namespace CoreJS.MetadataCell

/-- The internal name is not a JavaScript identifier or an entry export. -/
abbrev slot := NativeRecord.slot

def path (first second : String) : Expr :=
  .get (.get (.var slot) (.str first)) (.str second)

/-- Executing the deferred body through a captured receiver is two ordinary
property operations. The second runs in the state returned by the first.
No purity, data descriptor, host profile, or normal-completion premise. -/
theorem eval_captured {s : State W} {env : Env} {names : List Name}
    (captured : slot ∈ names) (bound : DataProjection.ReceiverAt s env slot cell receiver record)
    (first second : String) (n : Nat) :
    evalExpr (n + 3) (env.capture names) (path first second) s =
      ((do
        let v ← getProp (callFn (n + 1)) (.obj receiver) (.str first)
        getProp (callFn (n + 2)) v (.str second)) : M W Val) s := by
  have found : (env.capture names).vars.lookup slot = some cell := by
    simpa only [Env.capture, lookup_filterMap, List.mem_eraseDups, captured, ite_true] using bound.lookup
  obtain ⟨mutable, hb⟩ := bound.binding
  have read : readVar (env.capture names) slot s = .ok (.obj receiver) s := by
    simp only [readVar, found, M.bind_apply, getState, hb]
    rfl
  have key (label : String) (state : State W) :
      (pure (.str label) : M W Val) state = .ok (.str label) state := rfl
  simp only [path, evalExpr, M.bind_apply, read, key]

/-- Executable consumer check: look for the actual immutable cell and object
in the ready link. A missing, TDZ, mutable, primitive or dangling root fails. -/
def root? (env : Env) (s : State W) :
    Option ((root : Addr × Addr) × PLift (∃ obj,
      DataProjection.ReceiverAt s env slot root.1 root.2 obj ∧
      s.cellAt root.1 = some ⟨some (.obj root.2), false⟩)) :=
  match found : env.vars.lookup slot with
  | none => none
  | some cell =>
    match bound : s.cellAt cell with
    | some ⟨some (.obj addr), false⟩ =>
      match live : s.objAt addr with
      | some obj => some ⟨(cell, addr), ⟨⟨obj, ⟨found, ⟨false, bound⟩, live⟩, bound⟩⟩⟩
      | none => none
    | _ => none

/-- Check the two closures' root cells and receivers in the current state. The
loader still has to establish that these slots designate the retained module.
Getter changes, exceptions and unknown results are part of the equality. -/
def checkRead (source candidate : Env) (names : List Name) (first second : String) (s : State W) :
    Option (PLift (∀ n,
      evalExpr (n + 3) (source.capture names) (path first second) s =
      evalExpr (n + 3) (candidate.capture names) (path first second) s)) := do
  let ⟨a, ha⟩ ← root? source s
  let ⟨b, hb⟩ ← root? candidate s
  if captured : slot ∈ names then
    if eq : a = b then
      return ⟨by
        subst b
        obtain ⟨_, before, _⟩ := ha.down
        obtain ⟨_, after, _⟩ := hb.down
        exact fun n => (eval_captured captured before first second n).trans (eval_captured captured after first second n).symm⟩
  none

/-- A linked entry containing an opaque deferred reader with a metadata
capture still has no producer for that capture. Adding a free name to the
reader is insufficient. -/
private def reader : Program :=
  Program.single [.opaque 0 [] [("read", 0, .ordinary false)] [slot] .world]

theorem missing_linked_root (W : World) :
    (match link reader (initialState W reader.regions) with
     | .ok [(_, env)] s => (some ((root? env s).map (·.1)) : Option (Option (Addr × Addr)))
     | _ => none) = some none := by
  cbv

private def addr (i : Nat) : Addr := ⟨1, i⟩
private def envAt (cell : Nat) : Env := ⟨[(slot, addr cell)], .undef, none⟩

/-- Two module records, two metadata roots. The first environment getter reads
a captured cell; the second enters unsupported code only when called. -/
private def fixture : State World.probe :=
  let data v := Property.data v true true true
  let getter := fun f => Property.accessor (.obj (addr f)) .undef true true
  let obj := fun ps => ({ proto := none, props := ps } : Obj)
  let body := Func.mk "env" .method false [] [.ret (.var "current")]
  let refused := Func.opaque "env" .method false 0 1 0 [] (some "outside model")
  { initialState World.probe 1 with
    heap := #[builtins, #[obj [("env", getter 2)], obj [("env", getter 3)],
      { proto := none, props := [], code := some (.closure body ⟨[("current", addr 2)], .undef, none⟩ []) },
      { proto := none, props := [], code := some (.closure refused (envAt 1) []) },
      obj [("MODE", data (.obj (addr 6)))], obj [("MODE", data (.obj (addr 7)))], obj [], obj []]]
    cells := #[#[], #[⟨some (.obj (addr 0)), false⟩, ⟨some (.obj (addr 1)), false⟩, ⟨some (.obj (addr 4)), true⟩]] }

#guard (checkRead (envAt 0) (envAt 0) [slot] "env" "MODE" fixture).isSome
#guard (checkRead (envAt 0) (envAt 1) [slot] "env" "MODE" fixture).isNone
#guard (checkRead (envAt 0) (envAt 0) [] "env" "MODE" fixture).isNone

private def read (env : Env) := evalExpr (W := World.probe) 12 (env.capture [slot]) (path "env" "MODE")

-- Invoke the same closure after a write; it must return a different object.
#guard match read (envAt 0) fixture with
  | .ok (.obj a) s => a == addr 6 && match setCell (addr 2) (.obj (addr 5)) s with
    | .ok _ t => match read (envAt 0) t with
      | .ok (.obj b) _ => b == addr 7
      | _ => false
    | _ => false
  | _ => false

-- Checking receiver identity doesn't run the getter or certify completion.
#guard (checkRead (envAt 1) (envAt 1) [slot] "env" "MODE" fixture).isSome
#guard match read (envAt 1) fixture with
  | .unknown why _ => why == "deferred function env: outside model"
  | _ => false

end CoreJS.MetadataCell
