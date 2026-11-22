import EsmSplit.CoreJS.WrapperMove

namespace EsmSplit

structure WrapperSwapShape where
  importAlias : String
  providerExport : String
  providerBinding : String
  factoryParams : List String
  resultBinding : String
  movedBinding : String
  movedKind : CoreJS.DeclKind
  earlier : Nat
  later : Nat
  increasing : earlier < later
  deriving Repr

/-- Interpret the recognized creation operations in a common linked environment.
Quantifying over the deferred functions avoids interpreting their bodies during this swap. -/
theorem WrapperSwapShape.normal_iff (q : WrapperSwapShape) {W : CoreJS.World} {s : CoreJS.State W}
    (env closed : CoreJS.Env) (cell factory : CoreJS.Addr) (name : CoreJS.Name)
    (returned callback moved : CoreJS.Func) (fuel : Nat)
    (provider : CoreJS.WrapperAt s env q.importAlias cell factory closed name q.factoryParams returned)
    (resultOwn : ∀ c, env.vars.lookup q.resultBinding = some c → c.region = q.earlier + 1)
    (movedOwn : ∀ c, env.vars.lookup q.movedBinding = some c → c.region = q.later + 1)
    (cellOther : cell.region ≠ q.later + 1) (factoryOther : factory.region ≠ q.later + 1) :
    let a := CoreJS.runUnit fuel env q.earlier (.decl .var q.resultBinding (.call (.var q.importAlias) [.func callback]))
    let b := CoreJS.runUnit fuel env q.later (.decl q.movedKind q.movedBinding (.func moved))
    ∀ f v t, (∃ u, a s = .ok f u ∧ b u = .ok v t) ↔ (∃ u, b s = .ok v u ∧ a u = .ok f t) := by
  have localMove := CoreJS.decl_local (W := W) (R := fun _ => False) (e := .func moved)
    rfl fuel env q.movedKind q.movedBinding (by intro z hz; cases hz) movedOwn
  exact CoreJS.wrapper_var_commutes_at provider resultOwn (Nat.ne_of_lt q.increasing) localMove
    (fun _ h => h.elim) cellOther factoryOther fuel callback

end EsmSplit
