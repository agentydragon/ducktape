import EsmSplit.CoreJS.HostProfile
import EsmSplit.CoreJS.Scope

/-! Two limits on modeling empty native WeakMap construction with the current
evaluator. Neither theorem adds an execution rule or assumes a pristine realm. -/

namespace CoreJS.WeakMapBoundary

/-- Even an effect-free lookup of the exact constructor identity does not give
the world a construct operation. At sufficient fuel the entire expression
returns unknown with its input state, for every world satisfying the lookup. -/
theorem stableRead_newEmpty {w : World} {identity : String}
    (unsupported : identity ≠ "window.Error") (h : w.StableRead (.binding "WeakMap") (.identity identity)) (n : Nat) (env : Env) :
    evalExpr (W := w) (n + 3) env (.new (.prim (.globalRef "WeakMap") []) []) =
      unknown s!"host value {identity} is constructed" := by
  have binding : (readHostRef (.binding "WeakMap") : M w Val) = pure (.host identity) := by
    funext s
    simp only [readHostRef, h]
    rfl
  change (readHostRef (.binding "WeakMap") >>= fun ctor =>
    constructTarget (n + 2) ctor ctor [] : M w Val) = _
  rw [binding, M.pure_bind]
  simp [constructTarget]

/-- The represented fields of an otherwise empty noncallable object.
These describe both a new native weak map and an ordinary lookalike, even if
both prototypes have been changed to null. -/
private def PlainShape (proto : Option Addr) (o : Obj) : Prop :=
  o.proto = proto ∧ o.props = [] ∧ o.code = none ∧ o.array = false ∧ o.regexp = none ∧ o.errorIdentity = false

/-- No brand observation based on these object records alone can accept the
native receiver while rejecting the ordinary one. This is a statement about
the present representation, not about an evaluator with another internal slot
or an address-indexed table. -/
theorem sameShape_sameObservation (p : Option Addr) (a b : Obj)
    (ha : PlainShape p a) (hb : PlainShape p b) (observe : Obj → Bool) :
    observe a = observe b := by
  cases a
  cases b
  simp_all [PlainShape]

end CoreJS.WeakMapBoundary
