import Experiments.LocalCall
import Experiments.QuietCrossing

/-! A decidable, model-only identity-call summary.

MiniJS has no JavaScript assignment, so the only changes to a global binding
are through statement declarations. `stableName` refuses all non-hoisted
declarations of the callee. This says nothing about the JavaScript frontend,
external code, or whether a JavaScript function has the MiniJS meaning.
-/

open EsmSplit

namespace MJS

/-- No executable statement in the program can overwrite this binding. -/
def stableName (P : List MStmt) (fn : Name) : Bool :=
  P.all fun s => !s.defs.contains fn || s.hoisted

/-- Infer only a direct call of a known, immutable hoisted identity function
on a numeric literal. Candidates are expected to be derived from syntax. -/
def inferIdentity (P : List MStmt) (j : Nat) (fn param : Name) (k : Int) : Bool :=
  (P[j]?.map (·.e) == some (.app (.gvar fn) (.num k))) &&
    (G0 P fn == some (.lam param (.lvar param))) && stableName P fn

structure IdentityCandidate where
  fn : Name
  param : Name
  argument : Int

/-- Derive the candidate names and argument from the modeled program itself.
The returned value carries the decidable inference evidence, not a supplied
semantic certificate. Unsupported modeled expressions are refused. -/
def deriveIdentity (P : List MStmt) (j : Nat) :
    Option { c : IdentityCandidate // inferIdentity P j c.fn c.param c.argument = true } :=
  match P[j]? with
  | some st =>
    match st.e with
    | .app (.gvar fn) (.num k) =>
      match G0 P fn with
      | some (.lam param _) =>
        if h : inferIdentity P j fn param k = true then
          some ⟨⟨fn, param, k⟩, h⟩
        else none
      | _ => none
    | _ => none
  | _ => none

theorem stable_cstep (P : List MStmt) (fn : Name) (hs : stableName P fn = true)
    (fuel j : Nat) (G : Env) (w : World) :
    (cstep P fuel j (G, w)).1 fn = G fn := by
  cases hj : P[j]? with
  | none => simp [cstep, hj]
  | some s =>
    have hmem : s ∈ P := List.mem_of_getElem? hj
    have hstatic : !s.defs.contains fn || s.hoisted = true := by
      exact (List.all_eq_true.mp hs) s hmem
    by_cases hhoist : s.hoisted = true
    · simp [cstep, hj, hhoist]
    · have hnot : fn ∉ s.defs := by
        simpa [hhoist, List.contains_iff_mem] using hstatic
      cases heval : eval G fuel s.e w <;> simp [cstep, hj, hhoist, heval, hnot]

theorem stable_exec (P : List MStmt) (fn : Name) (hs : stableName P fn = true)
    (fuel : Nat) (ord : List Nat) (G : Env) (w : World) :
    (ord.foldl (fun s j => cstep P fuel j s) (G, w)).1 fn = G fn := by
  induction ord generalizing G w with
  | nil => rfl
  | cons j rest ih =>
    simp only [List.foldl_cons]
    cases h : cstep P fuel j (G, w) with
    | mk G' w' =>
      rw [ih]
      have := stable_cstep P fn hs fuel j G w
      simpa [h] using this

/-- The inferred call stays quiet after any order of MiniJS steps. There is no
user-supplied quiet certificate: the three premises are decidable program facts.
The theorem is about MiniJS, not ECMAScript. -/
theorem inferred_identity_reachable_quiet (P : List MStmt) (j : Nat) (fn param : Name) (k : Int)
    (h : inferIdentity P j fn param k = true) (fuel : Nat) (ord : List Nat) (w : World) :
    let G := (ord.foldl (fun s i => cstep P fuel i s) (G0 P, w)).1
    ∀ trace, P[j]?.bind (fun st => eval G fuel st.e trace) =
      (P[j]?.bind (fun st => eval G fuel st.e [])).map (fun r => (r.1, trace)) := by
  simp only [inferIdentity, Bool.and_eq_true, beq_iff_eq] at h
  obtain ⟨⟨hform, hinit⟩, hstable⟩ := h
  intro G trace
  have hG : G fn = some (.lam param (.lvar param)) := by
    dsimp [G]
    rw [stable_exec P fn hstable fuel ord (G0 P) w]
    exact hinit
  cases hj : P[j]? with
  | none => simp [hj] at hform
  | some st =>
    have he : st.e = .app (.gvar fn) (.num k) := by simpa [hj] using hform
    simpa [hj, he] using local_call_world G fn param (.lvar param) k hG rfl fuel trace

/-- A call to an identity lambda evaluates its numeric argument at enough fuel.
MiniJS treats failed or fuel-exhausted evaluation as `none`. -/
def identityResult (fuel : Nat) (k : Int) : Option Expr :=
  match fuel with
  | 0 | 1 => none
  | _ + 2 => some (.num k)

theorem eval_identity (G : Env) (fn param : Name) (k : Int)
    (hfn : G fn = some (.lam param (.lvar param))) (fuel : Nat) (w : World) :
    eval G fuel (.app (.gvar fn) (.num k)) w = (identityResult fuel k).map (fun v => (v, w)) := by
  cases fuel with
  | zero => rfl
  | succ fuel =>
    cases fuel with
    | zero => simp [eval, identityResult]
    | succ fuel => simp [eval, identityResult, hfn, Expr.subst]

/-- At a statically checked identity call, fix the abstract meaning to the
known result. Adequacy below is restricted to states reachable from `G0`. -/
def anchoredMeaning (P : List MStmt) (fuel j : Nat) (k : Int) : Meaning (Option Expr) World where
  pure i vs := if i == j then identityResult fuel k else (meaning P fuel).pure i vs
  run i vs w := if i == j then (identityResult fuel k, w) else (meaning P fuel).run i vs w

theorem anchored_quiet (P : List MStmt) (fuel j : Nat) (k : Int) :
    QuietCertificates (anchoredMeaning P fuel j k) (fun i => i == j) := by
  intro i hi args world
  have hij : i = j := by simpa using hi
  subst i
  simp [anchoredMeaning]

/-- The anchored abstract step agrees with the MiniJS evaluator if the callee
still has the statically inferred value. Other steps use the existing adequacy
proof and therefore require its closed-read and reachable-state invariants. -/
theorem anchored_step {P : List MStmt} {j : Nat} {fn param : Name} {k : Int}
    (hinfer : inferIdentity P j fn param k = true) (hc : ReadsClosed (toBundle P))
    (fuel i : Nat) (hi : i < P.length) {G : Env} (hInv : Inv P G)
    (hfixed : G fn = G0 P fn) (w : World) :
    (toBundle P).step (anchoredMeaning P fuel j k) i ⟨G, w⟩ =
      ⟨(cstep P fuel i (G, w)).1, (cstep P fuel i (G, w)).2⟩ := by
  by_cases hidx : i = j
  · subst i
    simp only [inferIdentity, Bool.and_eq_true, beq_iff_eq] at hinfer
    obtain ⟨⟨hform, hinit⟩, _⟩ := hinfer
    obtain ⟨st, hj⟩ : ∃ st, P[j]? = some st := ⟨P[j], by simp [hi]⟩
    have he : st.e = .app (.gvar fn) (.num k) := by simpa [hj] using hform
    have hst : P[j] = st := by simpa [hi] using hj
    have hB : (toBundle P).stmt j = st.fp := by rw [stmt_toBundle P hi, hst]
    have hG : G fn = some (.lam param (.lvar param)) := hfixed.trans hinit
    have hev := eval_identity G fn param k hG fuel w
    by_cases hh : st.hoisted = true
    · simp [Bundle.step, hB, MStmt.fp, hh, cstep, hj]
    · have hh' : st.hoisted = false := by simpa using hh
      simp only [Bundle.step, hB, MStmt.fp, Stmt.binds, List.append_nil, hh', Bool.false_eq_true,
        ↓reduceIte, he, Expr.inert, Bool.not_false, anchoredMeaning, beq_self_eq_true, meaning, hj, cstep]
      rw [hev]
      cases identityResult fuel k <;> simp
  · have hbase := step_adequate hc fuel hi hInv w
    simpa [Bundle.step, anchoredMeaning, hidx] using hbase

/-- Lift the step bridge through any valid order while preserving both the
existing reachability invariant and the automatically checked stable binding. -/
theorem anchored_exec {P : List MStmt} {j : Nat} {fn param : Name} {k : Int}
    (hinfer : inferIdentity P j fn param k = true) (hc : ReadsClosed (toBundle P)) (fuel : Nat)
    (ord : List Nat) (hlt : ∀ i ∈ ord, i < P.length) (G : Env) (w : World)
    (hInv : Inv P G) (hfixed : G fn = G0 P fn) :
    (toBundle P).exec (anchoredMeaning P fuel j k) ⟨G, w⟩ ord =
      ⟨(ord.foldl (fun s i => cstep P fuel i s) (G, w)).1,
       (ord.foldl (fun s i => cstep P fuel i s) (G, w)).2⟩ := by
  have hstable : stableName P fn = true := by
    have hchecked := hinfer
    simp only [inferIdentity, Bool.and_eq_true] at hchecked
    exact hchecked.2
  induction ord generalizing G w with
  | nil => rfl
  | cons i rest ih =>
    have hi : i < P.length := hlt i (List.mem_cons_self ..)
    rw [Bundle.exec_cons, anchored_step hinfer hc fuel i hi hInv hfixed w]
    exact ih (fun x hx => hlt x (List.mem_cons_of_mem _ hx))
      (cstep P fuel i (G, w)).1 (cstep P fuel i (G, w)).2
      (cstep_inv hc fuel hi hInv w) (by rw [stable_cstep P fn hstable fuel i G w]; exact hfixed)

/-- An inferred call may be proposed quiet in a plan without a handwritten
certificate: the certificate and reachable-state adequacy both follow from
the checked model facts. JavaScript-to-MiniJS remains outside this theorem. -/
theorem inferred_identity_plan_preserves {P : List MStmt} {j : Nat} {fn param : Name} {k : Int}
    (hinfer : inferIdentity P j fn param k = true) (hc : ReadsClosed (toBundle P))
    (hwf : WF P) (plan : Plan) (perm : Permutes (toBundle P) plan)
    (order : ConditionalOrderOk (toBundle P) plan (fun i => i == j)) (fuel : Nat) (w0 : World) :
    plan.execOrder.foldl (fun s i => cstep P fuel i s) (G0 P, w0) =
      (List.range P.length).foldl (fun s i => cstep P fuel i s) (G0 P, w0) := by
  have hlt : ∀ i ∈ plan.execOrder, i < P.length := by
    intro i hi
    simpa [length_toBundle] using perm.2.1 i hi
  have e1 := anchored_exec hinfer hc fuel plan.execOrder hlt (G0 P) w0 (inv_G0 hwf) rfl
  have e2 := anchored_exec hinfer hc fuel (List.range P.length)
    (fun i hi => List.mem_range.mp hi) (G0 P) w0 (inv_G0 hwf) rfl
  have e3 := conditional_plan_preserves (toBundle P) plan (anchoredMeaning P fuel j k)
    (fun i => i == j) (anchored_quiet P fuel j k) ⟨G0 P, w0⟩ perm order
  rw [length_toBundle, e1, e2] at e3
  exact Prod.ext (congrArg St.env e3) (congrArg St.world e3)

/-- The same plan theorem with candidates discovered from the modeled source;
no binding name, parameter, argument or quiet certificate is supplied by a user. -/
theorem derived_identity_plan_preserves {P : List MStmt} {j : Nat}
    (hderived : (deriveIdentity P j).isSome = true) (hc : ReadsClosed (toBundle P))
    (hwf : WF P) (plan : Plan) (perm : Permutes (toBundle P) plan)
    (order : ConditionalOrderOk (toBundle P) plan (fun i => i == j)) (fuel : Nat) (w0 : World) :
    plan.execOrder.foldl (fun s i => cstep P fuel i s) (G0 P, w0) =
      (List.range P.length).foldl (fun s i => cstep P fuel i s) (G0 P, w0) := by
  cases h : deriveIdentity P j with
  | none => simp [h] at hderived
  | some candidate =>
    exact inferred_identity_plan_preserves candidate.property hc hwf plan perm order fuel w0

namespace InferredIdentityExample

def program : List MStmt := [
  ⟨["identity"], .lam "x" (.lvar "x"), true⟩,
  ⟨["result"], .app (.gvar "identity") (.num 1), false⟩,
  ⟨["logged"], .log (.num 2), false⟩
]

theorem accepted : inferIdentity program 1 "identity" "x" 1 = true := by decide +kernel

theorem automatically_discovered : (deriveIdentity program 1).isSome = true := by decide +kernel

/-- Rebinding the callee is not an inferred identity call. -/
theorem reassigned_rejected : inferIdentity
    (program ++ [⟨["identity"], .lam "x" (.log (.lvar "x")), false⟩])
    1 "identity" "x" 1 = false := by decide +kernel

theorem effectful_body_rejected : inferIdentity
    [⟨["identity"], .lam "x" (.log (.lvar "x")), true⟩,
     ⟨["result"], .app (.gvar "identity") (.num 1), false⟩]
    1 "identity" "x" 1 = false := by decide +kernel

theorem effectful_argument_rejected : inferIdentity
    [⟨["identity"], .lam "x" (.lvar "x"), true⟩,
     ⟨["result"], .app (.gvar "identity") (.log (.num 1)), false⟩]
    1 "identity" "x" 1 = false := by decide +kernel

/-- A concrete effectful `log` crosses the inferred identity call. -/
def crossingPlan : Plan := {
  entry := "main",
  mods := [{ name := "main", stmts := [0, 2, 1], imports := [], exports := [] }]
}

theorem crossing_perm : Permutes (toBundle program) crossingPlan := by decide +kernel

theorem crossing_conditional :
    ConditionalOrderOk (toBundle program) crossingPlan (fun i => i == 1) := by decide +kernel

theorem crossing_without_inference_rejected : ¬ OrderOk (toBundle program) crossingPlan := by decide +kernel

theorem crossing_wf : WF program := by
  intro st hst hh
  simp [program] at hst
  rcases hst with rfl | rfl | rfl <;> simp_all

theorem crossing_preserves (fuel : Nat) (w0 : World) :
    crossingPlan.execOrder.foldl (fun s i => cstep program fuel i s) (G0 program, w0) =
      (List.range program.length).foldl (fun s i => cstep program fuel i s) (G0 program, w0) := by
  apply derived_identity_plan_preserves automatically_discovered (by decide +kernel) crossing_wf crossingPlan
  · exact crossing_perm
  · exact crossing_conditional

end InferredIdentityExample

end MJS
