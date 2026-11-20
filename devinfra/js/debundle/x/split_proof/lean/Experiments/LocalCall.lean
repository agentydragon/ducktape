import Experiments.MiniJS

/-! A narrow local-call effect rule in the MiniJS model.

This deliberately analyzes only a known closure with an inert body called with a numeric
literal. The run-time binding must equal the summarized closure. Nothing here analyzes real
JavaScript, generates summaries from source, or proves that a binding is immutable.
-/

open EsmSplit

namespace MJS

/-- Substitution of a number into an inert expression remains inert. -/
theorem inert_subst_num (body : Expr) (x : Name) (k : Int)
    (h : body.inert = true) : (body.subst x (.num k)).inert = true := by
  cases body with
  | num n => simp [Expr.subst, Expr.inert]
  | gvar g => simp [Expr.subst, Expr.inert]
  | lvar y => by_cases he : y = x <;> simp [Expr.subst, Expr.inert, he]
  | lam y b => by_cases he : y = x <;> simp [Expr.subst, Expr.inert, he]
  | app f a => simp [Expr.inert] at h
  | log e => simp [Expr.inert] at h

/-- A known closure with an inert body, called on a numeric literal, cannot modify the
MiniJS world. The binding equality is an explicit precondition, not a static analysis. -/
theorem local_call_world (G : Env) (fn x : Name) (body : Expr) (k : Int)
    (hfn : G fn = some (.lam x body)) (hbody : body.inert = true)
    (fuel : Nat) (w : World) :
    eval G fuel (.app (.gvar fn) (.num k)) w =
      (eval G fuel (.app (.gvar fn) (.num k)) []).map (fun r => (r.1, w)) := by
  cases fuel with
  | zero => simp [eval]
  | succ fuel =>
    cases fuel with
    | zero => simp [eval]
    | succ fuel =>
      simpa [eval, hfn, Function.comp_def] using
        (eval_inert_world (inert_subst_num body x k hbody) G (fuel + 1) w)

/-- A candidate summary is accepted only for a known closure and a literal argument.
The caller must establish that the candidate still equals the run-time binding. -/
def localCallInert (candidate : Option Expr) (arg : Expr) : Bool :=
  match candidate, arg with
  | some (.lam _ body), .num _ => body.inert
  | _, _ => false

/-- Soundness of the local-call classifier for MiniJS world effects. -/
theorem local_call_inert_sound (G : Env) (fn : Name) (candidate : Option Expr)
    (arg : Expr) (hbinding : G fn = candidate) (h : localCallInert candidate arg = true)
    (fuel : Nat) (w : World) :
    eval G fuel (.app (.gvar fn) arg) w =
      (eval G fuel (.app (.gvar fn) arg) []).map (fun r => (r.1, w)) := by
  cases candidate with
  | none => simp [localCallInert] at h
  | some candidate =>
    cases candidate <;> cases arg <;> simp_all [localCallInert]
    exact local_call_world G fn _ _ _ hbinding h fuel w

/-- The existing closed-read invariant bounds all global names read by the call, including
the callee's body. This does not prove that a JavaScript parser or resolver computes `R`. -/
theorem local_call_reads (fuel : Nat) (fn : Name) (k : Int) (w : World)
    (R : List Name) (G G' : Env) (hfn : fn ∈ R) (hG : Ok R G) (hG' : Ok R G')
    (hag : ∀ u ∈ R, G u = G' u) :
    eval G fuel (.app (.gvar fn) (.num k)) w =
      eval G' fuel (.app (.gvar fn) (.num k)) w := by
  apply eval_local_eq fuel _ w R G G' _ hG hG' hag
  simpa [Expr.fvg] using hfn

/-- An effectful callee is not eligible for this rule. -/
theorem effectful_body_rejected : (Expr.log (.num 1)).inert = false := by decide

theorem effectful_argument_rejected :
    localCallInert (some (.lam "x" (.lvar "x"))) (.log (.num 1)) = false := by decide

theorem effectful_call_changes_world :
    eval (fun _ => some (.lam "x" (.log (.lvar "x")))) 4
      (.app (.gvar "f") (.num 1)) [] = some (.num 0, [1]) := by decide

theorem inert_identity_accepted :
    localCallInert (some (.lam "x" (.lvar "x"))) (.num 1) = true := by decide

/-- A call through a changed binding is not covered by the known-closure precondition. -/
theorem changed_binding_rejected :
    (some (Expr.lam "x" (.log (.lvar "x"))) : Option Expr) ≠ some (.lam "x" (.lvar "x")) := by decide

end MJS
