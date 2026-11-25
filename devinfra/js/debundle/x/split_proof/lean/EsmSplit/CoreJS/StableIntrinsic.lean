import EsmSplit.CoreJS.Move
import EsmSplit.CoreJS.HostProfile

namespace CoreJS

/-- A stable global binding and one stable data property of its receiver. -/
structure StableIntrinsic where
  id : String
  binding : String
  owner : String
  key : String
  value : HostValue
  deriving DecidableEq

namespace StableIntrinsic

def mathPI : StableIntrinsic := ⟨"Math.PI@1", "Math", "window.Math", "PI", .number 0x400921fb54442d18⟩

def objectDefineProperty : StableIntrinsic :=
  ⟨"Object.defineProperty@1", "Object", "intrinsic.Object", "defineProperty", .identity "intrinsic.Object.defineProperty"⟩

/-- Neither observation changes host state. The binding must keep returning the
same receiver throughout initialization, including across retained units. -/
def Satisfied (d : StableIntrinsic) (w : World) : Prop :=
  w.StableRead (.binding d.binding) (.identity d.owner) ∧
  w.StableRead (.property d.owner d.key) d.value

abbrev Worlds (ds : List StableIntrinsic) (w : World) : Prop := ∀ d ∈ ds, d.Satisfied w

def assumption (d : StableIntrinsic) : String :=
  let result := match d.value with
    | .identity name => s!"the stable host reference {name}"
    | .number bits => s!"the Number with binary64 bits {bits.toNat}"
  s!"{d.id}: Throughout initialization, reading the ambient binding {d.binding} returns {d.owner}, and reading its own data property {d.key} returns {result}; both reads have no observable effects."

def readExpr (d : StableIntrinsic) : Expr :=
  .get (.prim (.globalRef d.binding) []) (.str d.key)

/-- The ordinary property evaluator must dispatch this descriptor to the
world's receiver/key observation, not to an intrinsic method. -/
def dispatched (d : StableIntrinsic) : Bool :=
  !((hostNamespaces.lookup d.owner).getD []).contains d.key &&
    observedHostProperty d.owner d.key

private theorem pureRead {w : World} {ref value}
    (h : w.StableRead ref value) :
    (readHostRef ref : M w Val) = pure value.val := by
  funext s
  simp only [readHostRef, h]
  rfl

private theorem evalRead {d : StableIntrinsic} {w : World} (hw : d.Satisfied w)
    (route : d.dispatched = true) (env : Env) (fuel : Nat) :
    (evalExpr fuel env d.readExpr : M w Val) = outOfFuel ∨
    evalExpr (W := w) fuel env d.readExpr = pure d.value.val := by
  cases fuel with
  | zero => exact Or.inl rfl
  | succ n =>
    cases n with
    | zero => exact Or.inl rfl
    | succ n =>
      cases n with
      | zero => exact Or.inl rfl
      | succ n =>
        apply Or.inr
        have binding : (readHostRef (.binding d.binding) : M w Val) = pure (.host d.owner) := pureRead hw.1
        have property : (readHostRef (.property d.owner d.key) : M w Val) = pure d.value.val :=
          pureRead hw.2
        change (readHostRef (.binding d.binding) >>= fun base =>
          getProp (callFn _) base (.str d.key) : M w Val) = _
        rw [binding, M.pure_bind]
        change hostMember d.owner d.key = _
        simp only [dispatched, Bool.and_eq_true, Bool.not_eq_true'] at route
        simp only [hostMember, route.1, route.2, Bool.false_eq_true, ↓reduceIte, property]

private def readMatch (d : StableIntrinsic) : (e : Expr) → Option (PLift (e = d.readExpr))
  | .get (.prim (.globalRef name) []) (.str key) =>
    if h : name = d.binding ∧ key = d.key then some ⟨by rw [h.1, h.2]; rfl⟩ else none
  | _ => none

/-- Use existing evaluator semantics for division, after both operands have
been proved Numbers. No rewriting of the initializer or coercion is allowed. -/
def evalValue? (d : StableIntrinsic) (e : Expr) : Option (PLift (∀ w, d.Satisfied w →
    ∀ fuel env, (evalExpr fuel env e : M w Val) = outOfFuel ∨
      ∃ value : Val, evalExpr (W := w) fuel env e = pure value)) := do
  let ⟨route⟩ ← if h : d.dispatched = true then some (PLift.up h) else none
  if let some ⟨eq⟩ := d.readMatch e then
    return ⟨fun w hw fuel env => eq ▸ (evalRead hw route env fuel).imp_right (fun h => ⟨_, h⟩)⟩
  match he : e with
  | .binary .div left (.num denominator) =>
    match kind : d.value with
    | .identity _ => none
    | .number bits =>
      let ⟨eq⟩ ← d.readMatch left
      return ⟨by
        intro w hw fuel env
        rw [he, eq]
        cases fuel with
        | zero => exact Or.inl rfl
        | succ n =>
          cases n with
          | zero => exact Or.inl rfl
          | succ n =>
            rw [evalExpr]
            rcases evalRead hw route env (n + 1) with out | number
            · rw [out]; exact Or.inl rfl
            · rw [number, kind]; exact Or.inr ⟨_, rfl⟩⟩
  | _ => none

end StableIntrinsic
end CoreJS
