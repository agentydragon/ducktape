import EsmSplit.Semantics

/-!
# A tiny but real JavaScript-like semantics: deriving the frame assumption

`Semantics.lean` assumes a statement's meaning depends only on the values of the names in its
footprint. Here is a small language with an actual evaluator, and a proof that the assumption holds.

The language is Turing-complete: closures, application, recursion through globals, and an effect
(`log`). Evaluation is fuel-bounded, so non-termination just means "no result at any fuel".
Parameters (`lvar`) and module-level names (`gvar`) are separate namespaces, so substituting a
closed value never captures anything.
-/

open EsmSplit

namespace MJS

inductive Expr where
  | num  (n : Int)
  | gvar (g : Name)
  | lvar (x : Name)
  | lam  (x : Name) (b : Expr)
  | app  (f a : Expr)
  | log  (e : Expr)
  deriving Repr, DecidableEq, Inhabited

/-- Module-level names mentioned anywhere. -/
def Expr.fvg : Expr → List Name
  | .num _ => []
  | .gvar g => [g]
  | .lvar _ => []
  | .lam _ b => b.fvg
  | .app f a => f.fvg ++ a.fvg
  | .log e => e.fvg

def Expr.subst : Expr → Name → Expr → Expr
  | .num n, _, _ => .num n
  | .gvar g, _, _ => .gvar g
  | .lvar y, x, v => if y == x then v else .lvar y
  | .lam y b, x, v => if y == x then .lam y b else .lam y (b.subst x v)
  | .app f a, x, v => .app (f.subst x v) (a.subst x v)
  | .log e, x, v => .log (e.subst x v)

/-- Module-level bindings (values only) and the console. -/
abbrev Env := Name → Option Expr
abbrev World := List Int

/-- Call-by-value, left to right. `fuel` bounds the depth of the derivation. -/
def eval (G : Env) : Nat → Expr → World → Option (Expr × World)
  | 0, _, _ => none
  | _ + 1, .num n, w => some (.num n, w)
  | _ + 1, .lam x b, w => some (.lam x b, w)
  | _ + 1, .gvar g, w => (G g).map (·, w)
  | _ + 1, .lvar _, _ => none
  | n + 1, .log e, w =>
    match eval G n e w with
    | some (.num k, w') => some (.num 0, w' ++ [k])
    | _ => none
  | n + 1, .app f a, w =>
    match eval G n f w with
    | some (.lam x b, w₁) =>
      match eval G n a w₁ with
      | some (v, w₂) => eval G n (b.subst x v) w₂
      | none => none
    | _ => none

theorem fvg_subst (x : Name) (v : Expr) :
    ∀ b : Expr, ∀ g ∈ (b.subst x v).fvg, g ∈ b.fvg ∨ g ∈ v.fvg := by
  intro b
  induction b with
  | num _ | gvar _ => simp [Expr.subst, Expr.fvg]
  | lvar y =>
    intro g hg
    by_cases h : y = x <;> simp_all [Expr.subst, Expr.fvg]
  | lam y b ih =>
    intro g hg
    by_cases h : y = x
    · simp_all [Expr.subst, Expr.fvg]
    · simp only [Expr.subst, beq_iff_eq, h, ite_false, Expr.fvg] at hg ⊢
      exact ih g hg
  | app f a ihf iha =>
    intro g hg
    simp only [Expr.subst, Expr.fvg, List.mem_append] at hg ⊢
    rcases hg with hg | hg
    · exact (ihf g hg).imp Or.inl id |>.elim Or.inl Or.inr
    · exact (iha g hg).elim (fun h => Or.inl (Or.inr h)) Or.inr
  | log e ih => intro g hg; simpa [Expr.subst, Expr.fvg] using ih g hg

/-- Every value stored under a name in `R` only mentions names in `R`. -/
def Ok (R : List Name) (G : Env) : Prop :=
  ∀ u ∈ R, ∀ v, G u = some v → ∀ g ∈ v.fvg, g ∈ R

/-- **Locality and coverage.** If `e` only mentions names of `R`, and two environments agree on `R`
and are `Ok` there, then whatever `e` computes in one it computes in the other, and the value it
computes only mentions names of `R`. -/
theorem eval_local : ∀ (n : Nat) (e : Expr) (w : World) (R : List Name) (G G' : Env),
    (∀ g ∈ e.fvg, g ∈ R) → Ok R G → Ok R G' → (∀ u ∈ R, G u = G' u) →
    ∀ r, eval G n e w = some r → eval G' n e w = some r ∧ ∀ g ∈ r.1.fvg, g ∈ R := by
  intro n
  induction n with
  | zero => intro e w R G G' _ _ _ _ r h; simp [eval] at h
  | succ n ih =>
    intro e w R G G' hfv hG hG' hag r h
    cases e with
    | num k => simp [eval] at h; subst h; simp [eval, Expr.fvg]
    | lvar x => simp [eval] at h
    | lam x b =>
      simp [eval] at h; subst h
      exact ⟨by simp [eval], fun g hg => hfv g hg⟩
    | gvar g =>
      have hgR := hfv g (by simp [Expr.fvg])
      simp only [eval, Option.map_eq_some_iff] at h
      obtain ⟨v, hv, rfl⟩ := h
      refine ⟨by simp [eval, ← hag g hgR, hv], fun g' hg' => hG g hgR v hv g' hg'⟩
    | log e =>
      simp only [eval] at h
      split at h
      · rename_i k w' hk
        simp at h; subst h
        obtain ⟨h1, _⟩ := ih e w R G G' (by simpa [Expr.fvg] using hfv) hG hG' hag _ hk
        simp [eval, h1, Expr.fvg]
      · simp at h
    | app f a =>
      simp only [eval] at h
      have hfa : (∀ g ∈ f.fvg, g ∈ R) ∧ (∀ g ∈ a.fvg, g ∈ R) := by
        refine ⟨fun g hg => hfv g ?_, fun g hg => hfv g ?_⟩ <;> simp [Expr.fvg, hg]
      split at h
      · rename_i x b w₁ hf
        obtain ⟨hf', hfcov⟩ := ih f w R G G' hfa.1 hG hG' hag _ hf
        split at h
        · rename_i v w₂ ha
          obtain ⟨ha', havcov⟩ := ih a w₁ R G G' hfa.2 hG hG' hag _ ha
          have hbody : ∀ g ∈ (b.subst x v).fvg, g ∈ R := fun g hg =>
            (fvg_subst x v b g hg).elim (fun h => hfcov g h) (fun h => havcov g h)
          obtain ⟨hb1, hb2⟩ := ih _ w₂ R G G' hbody hG hG' hag _ h
          exact ⟨by simp [eval, hf', ha', hb1], hb2⟩
        · simp at h
      · simp at h


/-! ## Statements and footprints

A statement declares some names, has an initializer, and may be a hoisted function declaration.
The footprint is computed from the syntax, the way `extract.mjs` does for JavaScript:
a statement that calls anything may call any closure it mentions, so there all names are eager. -/

structure MStmt where
  defs    : List Name
  e       : Expr
  hoisted : Bool
  deriving DecidableEq

def Expr.eagerG : Expr → List Name
  | .gvar g => [g]
  | .app f a => f.eagerG ++ a.eagerG
  | .log e => e.eagerG
  | _ => []

def Expr.lazyG : Expr → List Name
  | .lam _ b => b.fvg
  | .app f a => f.lazyG ++ a.lazyG
  | .log e => e.lazyG
  | _ => []

/-- Evaluating it creates values and reads variables; it calls nothing. -/
def Expr.inert : Expr → Bool
  | .app .. | .log _ => false
  | _ => true

def MStmt.fp (s : MStmt) : Stmt :=
  { src := "", defs := s.defs, hoisted := s.hoisted, effect := !s.e.inert, lazy := s.e.lazyG,
    eager := if s.e.inert then s.e.eagerG else s.e.fvg }

def toBundle (P : List MStmt) : Bundle := P.map MStmt.fp

/-! ## Reachable states

Every value stored under a name only mentions names in the footprint of the statement that
declared it. Hoisted function values satisfy this at the start; every step preserves it. -/

def Inv (P : List MStmt) (G : Env) : Prop :=
  ∀ u v, G u = some v → ∀ g ∈ v.fvg, ∃ i < P.length, u ∈ ((toBundle P).stmt i).defs ∧
    (g ∈ (toBundle P).reads i ∨ g ∈ ((toBundle P).stmt i).lazy)

theorem length_toBundle (P : List MStmt) : (toBundle P).length = P.length := by simp [toBundle]

theorem stmt_toBundle (P : List MStmt) {j : Nat} (hj : j < P.length) :
    (toBundle P).stmt j = P[j].fp := by
  simp [toBundle, Bundle.stmt, hj]

theorem reads_defined {P : List MStmt} (hc : ReadsClosed (toBundle P)) {j : Nat} (hj : j < P.length) :
    ((toBundle P).readsSet (toBundle P).index j).isSome = true := by
  rw [← readsSetWith_eq]
  exact hc j (List.mem_range.2 (by simpa [length_toBundle] using hj))

theorem closed_reads {P : List MStmt} (hc : ReadsClosed (toBundle P)) {j : Nat} (hj : j < P.length) :
    (toBundle P).Closed (· ∈ (toBundle P).reads j) := Bundle.reads_closed (reads_defined hc hj)

/-- With a closed read set, stored values stay inside it. -/
theorem ok_reads {P : List MStmt} (hc : ReadsClosed (toBundle P)) {G : Env} (hG : Inv P G)
    {j : Nat} (hj : j < P.length) : Ok ((toBundle P).reads j) G := by
  intro u hu v hv g hg
  obtain ⟨i, hi, hdef, hg'⟩ := hG u v hv g hg
  have hdecl : (toBundle P).stmt i ∈ (toBundle P).declarers u := by
    simp only [Bundle.declarers, List.mem_filter]
    refine ⟨?_, by simpa using hdef⟩
    rw [stmt_toBundle P hi]
    exact List.mem_map.2 ⟨_, List.getElem_mem hi, rfl⟩
  have hcl := closed_reads hc hj u hu _ hdecl
  rcases hg' with h | h
  · exact Bundle.reads_least (closed_reads hc hj)
      (fun _ w hw => hcl w (List.mem_append.2 (Or.inl (by simpa [stmt_toBundle P hi, MStmt.fp] using hw)))) g h
  · exact hcl g (List.mem_append.2 (Or.inr h))

/-- Initial bindings: the hoisted function declarations. -/
def G0 (P : List MStmt) : Env :=
  fun u => (P.find? fun st => st.hoisted && st.defs.contains u).map (·.e)

/-- Hoisted statements are function expressions. -/
def WF (P : List MStmt) : Prop := ∀ st ∈ P, st.hoisted = true → ∃ p b, st.e = .lam p b

theorem inv_G0 {P : List MStmt} (hwf : WF P) : Inv P (G0 P) := by
  intro u v hv g hg
  simp only [G0, Option.map_eq_some_iff, List.find?_eq_some_iff_getElem] at hv
  obtain ⟨st, ⟨hp, i, hi, rfl, _⟩, rfl⟩ := hv
  simp only [Bool.and_eq_true, List.contains_iff_mem] at hp
  obtain ⟨p, b, hb⟩ := hwf P[i] (List.getElem_mem hi) hp.1
  refine ⟨i, hi, ?_, Or.inr ?_⟩
  · rw [stmt_toBundle P hi]; exact hp.2
  · rw [stmt_toBundle P hi]; simp_all [MStmt.fp, Expr.lazyG, Expr.fvg]


/-! ## Running a program: the real step, and the abstract meaning of `Semantics.lean`

A stuck evaluation binds its names to "nothing" and leaves the world alone (poison). -/

def cstep (P : List MStmt) (fuel : Nat) (j : Nat) (s : Env × World) : Env × World :=
  match P[j]? with
  | none => s
  | some st =>
    if st.hoisted then s else
    match eval s.1 fuel st.e s.2 with
    | some (v, w') => (fun u => if st.defs.contains u then some v else s.1 u, w')
    | none => (fun u => if st.defs.contains u then none else s.1 u, s.2)

def envOf (rs : List Name) (vs : List (Option Expr)) : Env :=
  fun u => ((rs.zip vs).lookup u).getD none

/-- The meaning `Semantics.lean` assumes: a function of the values of the footprint names only. -/
def meaning (P : List MStmt) (fuel : Nat) : Meaning (Option Expr) World where
  pure j vs :=
    match P[j]? with
    | some st => (eval (envOf ((toBundle P).reads j) vs) fuel st.e []).map (·.1)
    | none => none
  run j vs w :=
    match P[j]? with
    | some st =>
      match eval (envOf ((toBundle P).reads j) vs) fuel st.e w with
      | some (v, w') => (some v, w')
      | none => (none, w)
    | none => (none, w)

theorem envOf_map (G : Env) : ∀ (rs : List Name) (u : Name), u ∈ rs → envOf rs (rs.map G) u = G u := by
  intro rs u hu
  induction rs with
  | nil => simp at hu
  | cons r rs ih =>
    by_cases h : u = r
    · subst h; simp [envOf, List.zip]
    · have hb : (u == r) = false := by simpa using h
      have := ih (by simpa [h] using hu)
      simpa [envOf, List.zip, List.lookup, hb] using this

theorem eval_local_eq (n : Nat) (e : Expr) (w : World) (R : List Name) (G G' : Env)
    (hfv : ∀ g ∈ e.fvg, g ∈ R) (hG : Ok R G) (hG' : Ok R G') (hag : ∀ u ∈ R, G u = G' u) :
    eval G n e w = eval G' n e w := by
  cases h : eval G n e w with
  | some r => exact ((eval_local n e w R G G' hfv hG hG' hag r h).1).symm
  | none =>
    cases h' : eval G' n e w with
    | none => rfl
    | some r =>
      have := (eval_local n e w R G' G hfv hG' hG (fun u hu => (hag u hu).symm) r h').1
      simp [h] at this

theorem eval_inert_world {e : Expr} (he : e.inert = true) (G : Env) (n : Nat) (w : World) :
    eval G n e w = (eval G n e []).map fun r => (r.1, w) := by
  cases e <;> cases n <;> simp_all [eval, Expr.inert, Function.comp_def]

theorem eval_inert_env {e : Expr} (he : e.inert = true) {G G' : Env}
    (h : ∀ g ∈ e.eagerG, G g = G' g) (n : Nat) (w : World) : eval G n e w = eval G' n e w := by
  cases e <;> cases n <;> simp_all [eval, Expr.inert, Expr.eagerG]


/-! ## Adequacy: the abstract meaning *is* the evaluator, on reachable states -/

/-- What statement `j` computes does not depend on anything outside its footprint. -/
theorem reconstruct {P : List MStmt} (hc : ReadsClosed (toBundle P)) {G : Env} (hG : Inv P G)
    {j : Nat} (hj : j < P.length) {st : MStmt} (hP : P[j]? = some st) (hh : st.hoisted = false)
    (w : World) (fuel : Nat) :
    eval (envOf ((toBundle P).reads j) (((toBundle P).reads j).map G)) fuel st.e w = eval G fuel st.e w := by
  have hst : P[j] = st := by simpa [hj] using hP
  have hB : (toBundle P).stmt j = st.fp := by rw [stmt_toBundle P hj, hst]
  have hR := ok_reads hc hG hj
  have hsuper := Bundle.reads_super (reads_defined hc hj) (by simp [hB, MStmt.fp, hh])
  generalize (toBundle P).reads j = R at hR hsuper ⊢
  have hag : ∀ u ∈ R, G u = envOf R (R.map G) u := fun u hu => (envOf_map G R u hu).symm
  have hG' : Ok R (envOf R (R.map G)) :=
    fun u hu v hv g hg => hR u hu v (by rw [hag u hu]; exact hv) g hg
  by_cases hi : st.e.inert = true
  · refine eval_inert_env hi (fun g hg => (hag g (hsuper g ?_)).symm) fuel w
    simpa [hB, MStmt.fp, hi] using hg
  · exact eval_local_eq fuel st.e w R _ G
      (fun g hg => hsuper g (by simp [hB, MStmt.fp, hi, hg])) hG' hR (fun u hu => (hag u hu).symm)

theorem step_adequate {P : List MStmt} (hc : ReadsClosed (toBundle P)) (fuel : Nat) {j : Nat}
    (hj : j < P.length) {G : Env} (hG : Inv P G) (w : World) :
    (toBundle P).step (meaning P fuel) j ⟨G, w⟩ =
      ⟨(cstep P fuel j (G, w)).1, (cstep P fuel j (G, w)).2⟩ := by
  obtain ⟨st, hP⟩ : ∃ st, P[j]? = some st := ⟨P[j], by simp [hj]⟩
  have hst : P[j] = st := by simpa [hj] using hP
  have hB : (toBundle P).stmt j = st.fp := by rw [stmt_toBundle P hj, hst]
  by_cases hh : st.hoisted = true
  · simp [Bundle.step, hB, MStmt.fp, hh, cstep, hP]
  · have hh' : st.hoisted = false := by simpa using hh
    have hrec := reconstruct hc hG hj hP hh'
    by_cases hi : st.e.inert = true
    · have hw := eval_inert_world hi G fuel w
      simp only [Bundle.step, hB, MStmt.fp, Stmt.binds, List.append_nil, hh', hi, meaning, hP, cstep,
        Bool.false_eq_true, ↓reduceIte, Bool.not_true, hrec, hw]
      cases h : eval G fuel st.e [] <;> simp
    · have hi' : st.e.inert = false := by simpa using hi
      simp only [Bundle.step, hB, MStmt.fp, Stmt.binds, List.append_nil, hh', hi', meaning, hP, cstep,
        Bool.false_eq_true, ↓reduceIte, Bool.not_false, hrec]
      cases h : eval G fuel st.e w <;> simp


/-! ## Reachable states stay reachable -/

theorem cstep_inv {P : List MStmt} (hc : ReadsClosed (toBundle P)) (fuel : Nat) {j : Nat}
    (hj : j < P.length) {G : Env} (hG : Inv P G) (w : World) : Inv P (cstep P fuel j (G, w)).1 := by
  obtain ⟨st, hP⟩ : ∃ st, P[j]? = some st := ⟨P[j], by simp [hj]⟩
  have hst : P[j] = st := by simpa [hj] using hP
  have hB : (toBundle P).stmt j = st.fp := by rw [stmt_toBundle P hj, hst]
  by_cases hh : st.hoisted = true
  · simpa [cstep, hP, hh] using hG
  · have hh' : st.hoisted = false := by simpa using hh
    have hR := ok_reads hc hG hj
    have hsuper := Bundle.reads_super (reads_defined hc hj) (by simp [hB, MStmt.fp, hh'])
    simp only [cstep, hP, hh', Bool.false_eq_true, ↓reduceIte]
    cases h : eval G fuel st.e w with
    | none =>
      intro u v hv g hg
      by_cases hd : u ∈ st.defs
      · simp [hd] at hv
      · simp [hd] at hv; exact hG u v hv g hg
    | some r =>
      obtain ⟨v₀, w'⟩ := r
      intro u v hv g hg
      by_cases hd : u ∈ st.defs
      · simp only [List.contains_iff_mem, hd, ↓reduceIte, Option.some.injEq] at hv
        subst hv
        refine ⟨j, hj, by simpa [hB, MStmt.fp] using hd, ?_⟩
        by_cases hi : st.e.inert = true
        · cases he : st.e with
          | num k =>
            cases fuel with
            | zero => simp [eval] at h
            | succ n =>
              simp [he, eval] at h
              obtain ⟨rfl, -⟩ := h
              simp [Expr.fvg] at hg
          | lvar x => cases fuel <;> simp [he, eval] at h
          | app f a => simp [he, Expr.inert] at hi
          | log e => simp [he, Expr.inert] at hi
          | lam x b =>
            cases fuel with
            | zero => simp [eval] at h
            | succ n =>
              simp [he, eval] at h
              obtain ⟨rfl, -⟩ := h
              exact Or.inr (by simpa [hB, MStmt.fp, he, Expr.lazyG, Expr.fvg] using hg)
          | gvar g0 =>
            cases fuel with
            | zero => simp [eval] at h
            | succ n =>
              simp only [he, eval, Option.map_eq_some_iff, Prod.mk.injEq] at h
              obtain ⟨a, ha, rfl, -⟩ := h
              have hg0 : g0 ∈ (toBundle P).reads j :=
                hsuper g0 (by simp [hB, MStmt.fp, he, Expr.inert, Expr.eagerG])
              exact Or.inl (hR g0 hg0 _ ha g hg)
        · exact Or.inl ((eval_local fuel st.e w _ G G
            (fun g hg => hsuper g (by simp [hB, MStmt.fp, hi, hg])) hR hR (fun _ _ => rfl) (v₀, w') h).2 g hg)
      · simp only [List.contains_iff_mem, hd, ↓reduceIte] at hv
        exact hG u v hv g hg

theorem cexec_eq {P : List MStmt} (hc : ReadsClosed (toBundle P)) (fuel : Nat) :
    ∀ (ord : List Nat), (∀ x ∈ ord, x < P.length) → ∀ (s : Env × World), Inv P s.1 →
      (toBundle P).exec (meaning P fuel) ⟨s.1, s.2⟩ ord =
        ⟨(ord.foldl (fun s j => cstep P fuel j s) s).1, (ord.foldl (fun s j => cstep P fuel j s) s).2⟩ := by
  intro ord
  induction ord with
  | nil => intro _ s _; rfl
  | cons j ord ih =>
    intro hlt s hs
    obtain ⟨G, w⟩ := s
    have hj : j < P.length := hlt j (List.mem_cons_self ..)
    rw [Bundle.exec_cons, step_adequate hc fuel hj hs w]
    exact ih (fun x hx => hlt x (List.mem_cons_of_mem _ hx)) (cstep P fuel j (G, w))
      (cstep_inv hc fuel hj hs w)

/-- **The theorem, for the real evaluator.** Run the program's statements in any order that
lists each once and keeps conflicting statements in bundle order. Every name ends with the same
value and the console shows the same output, at every fuel. -/
theorem mjs_reorder {P : List MStmt} (hc : ReadsClosed (toBundle P)) (hwf : WF P) (fuel : Nat)
    (ord : List Nat) (hnd : ord.Nodup) (hmem : ∀ x, x ∈ ord ↔ x < P.length)
    (hok : ord.Pairwise fun a b => a < b ∨ (toBundle P).conflict a b = false) :
    ord.foldl (fun s j => cstep P fuel j s) (G0 P, []) =
      (List.range P.length).foldl (fun s j => cstep P fuel j s) (G0 P, []) := by
  have h0 := inv_G0 hwf
  have e1 := cexec_eq hc fuel ord (fun x hx => (hmem x).1 hx) (G0 P, []) h0
  have e2 := cexec_eq hc fuel (List.range P.length) (fun x hx => List.mem_range.1 hx) (G0 P, []) h0
  have e3 := (toBundle P).exec_reorder (meaning P fuel) ⟨G0 P, []⟩ P.length ord hnd hmem hok
  rw [e1, e2] at e3
  exact Prod.ext (congrArg St.env e3) (congrArg St.world e3)

end MJS
