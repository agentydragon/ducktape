import Experiments.MiniJS

/-!
# Two tiers: automatic for the formulaic pairs, custom Lean for the rest

`reorder_gen` is the reordering theorem with the commutation test abstracted into an oracle
`comm`. For mini-JS the oracle has two parts:

* tier 1, automatic: the footprints do not conflict. `step_adequate` + `step_comm` prove the
  commutation; the per-instance obligation is `decide`.
* tier 2, custom: the user lists extra pairs `(a, b)` and proves `SemComm a b` in the real
  semantics, for statements the conservative footprint analysis cannot clear.
-/

open EsmSplit

namespace MJS

/-- The reordering theorem with an oracle. (Same proof as `Bundle.exec_reorder`, over any step function.) -/
theorem reorder_gen {S : Type} (step : Nat → S → S) (comm : Nat → Nat → Prop) (Inv : S → Prop)
    (hinv : ∀ j s, Inv s → Inv (step j s)) (N : Nat)
    (hcomm : ∀ i j s, i < N → j < N → Inv s → comm i j → step j (step i s) = step i (step j s))
    (s0 : S) (h0 : Inv s0) :
    ∀ (n : Nat), n ≤ N → ∀ (ord : List Nat), ord.Nodup → (∀ x, x ∈ ord ↔ x < n) →
      ord.Pairwise (fun a b => a < b ∨ comm a b) →
      ord.foldl (fun s j => step j s) s0 = (List.range n).foldl (fun s j => step j s) s0 := by
  have inv_fold : ∀ (l : List Nat) (s : S), Inv s → Inv (l.foldl (fun s j => step j s) s) := by
    intro l; induction l with
    | nil => intro s h; exact h
    | cons j l ih => intro s h; exact ih _ (hinv j s h)
  have move_past : ∀ (x : Nat) (q : List Nat), x < N → (∀ y ∈ q, y < N ∧ comm x y) → ∀ s, Inv s →
      q.foldl (fun s j => step j s) (step x s) = step x (q.foldl (fun s j => step j s) s) := by
    intro x q hx
    induction q with
    | nil => intro _ s _; rfl
    | cons y q ih =>
      intro hq s hs
      have hy := hq y (List.mem_cons_self ..)
      simp only [List.foldl]
      rw [hcomm x y s hx hy.1 hs hy.2]
      exact ih (fun z hz => hq z (List.mem_cons_of_mem _ hz)) _ (hinv y s hs)
  intro n
  induction n with
  | zero =>
    intro _ ord _ hmem _
    cases ord with
    | nil => rfl
    | cons a _ => exact absurd ((hmem a).1 (List.mem_cons_self ..)) (Nat.not_lt_zero a)
  | succ n ih =>
    intro hn ord hnd hmem hok
    obtain ⟨p, q, rfl⟩ := List.append_of_mem ((hmem n).2 (Nat.lt_succ_self n))
    have hnd' := List.nodup_append.1 hnd
    have hok' := List.pairwise_append.1 hok
    have hnq := List.nodup_cons.1 hnd'.2.1
    have hpq : ∀ x, x ∈ p ++ q ↔ x < n := by
      intro x
      have hx := hmem x
      have hxp : x ∈ p → x ≠ n := fun h e => hnd'.2.2 x h n (List.mem_cons_self ..) e
      have hxq : x ∈ q → x ≠ n := fun h e => hnq.1 (e ▸ h)
      simp only [List.mem_append, List.mem_cons] at hx ⊢
      constructor
      · intro h
        have := hx.1 (h.elim Or.inl fun h => Or.inr (Or.inr h))
        rcases h with h | h
        · exact Nat.lt_of_le_of_ne (Nat.lt_succ_iff.1 this) (hxp h)
        · exact Nat.lt_of_le_of_ne (Nat.lt_succ_iff.1 this) (hxq h)
      · intro h
        rcases hx.2 (Nat.lt_succ_of_lt h) with h' | h' | h'
        · exact Or.inl h'
        · omega
        · exact Or.inr h'
    have hq : ∀ y ∈ q, y < N ∧ comm n y := by
      intro y hy
      have hyn := (hpq y).1 (List.mem_append_right _ hy)
      refine ⟨by omega, ?_⟩
      rcases (List.pairwise_cons.1 hok'.2.1).1 y hy with h | h
      · omega
      · exact h
    have hperm : (p ++ n :: q).foldl (fun s j => step j s) s0
        = step n ((p ++ q).foldl (fun s j => step j s) s0) := by
      rw [List.foldl_append, List.foldl_cons, move_past n q (by omega) hq _ (inv_fold p s0 h0),
        List.foldl_append]
    rw [hperm, ih (by omega) (p ++ q) ?_ hpq ?_, List.range_succ, List.foldl_append]
    · rfl
    · exact List.nodup_append.2 ⟨hnd'.1, hnq.2, fun a ha b hb => hnd'.2.2 a ha b (List.mem_cons_of_mem _ hb)⟩
    · refine List.pairwise_append.2 ⟨hok'.1, (List.pairwise_cons.1 hok'.2.1).2, fun a ha b hb => ?_⟩
      exact hok'.2.2 a ha b (List.mem_cons_of_mem _ hb)

/-! ## Tier 1 for mini-JS: non-conflicting footprints commute on reachable states -/

theorem cstep_comm_auto {P : List MStmt} (hc : ReadsClosed (toBundle P)) (fuel : Nat) {i j : Nat}
    (hi : i < P.length) (hj : j < P.length) (h : (toBundle P).conflict i j = false)
    (s : Env × World) (hs : Inv P s.1) :
    cstep P fuel j (cstep P fuel i s) = cstep P fuel i (cstep P fuel j s) := by
  obtain ⟨G, w⟩ := s
  have a1 := step_adequate hc fuel hj (cstep_inv hc fuel hi hs w) (cstep P fuel i (G, w)).2
  have a2 := step_adequate hc fuel hi hs w
  have a3 := step_adequate hc fuel hi (cstep_inv hc fuel hj hs w) (cstep P fuel j (G, w)).2
  have a4 := step_adequate hc fuel hj hs w
  have key := (toBundle P).step_comm (meaning P fuel) h (⟨G, w⟩ : St (Option Expr) World)
  rw [← a2] at a1
  rw [← a4] at a3
  rw [a1, a3] at key
  exact Prod.ext (congrArg St.env key) (congrArg St.world key)

/-! ## Tier 2: a semantic commutation proved by hand -/

def SemComm (P : List MStmt) (fuel i j : Nat) : Prop :=
  ∀ s : Env × World, Inv P s.1 → cstep P fuel j (cstep P fuel i s) = cstep P fuel i (cstep P fuel j s)

/-- **Reordering with extras.** Pairs the footprints cannot clear (`extra`) come with a proof
that they commute in the real semantics. -/
theorem mjs_reorder_ext {P : List MStmt} (hc : ReadsClosed (toBundle P)) (hwf : WF P) (fuel : Nat)
    (extra : List (Nat × Nat)) (hextra : ∀ p ∈ extra, p.1 < P.length → p.2 < P.length → SemComm P fuel p.1 p.2)
    (ord : List Nat) (hnd : ord.Nodup) (hmem : ∀ x, x ∈ ord ↔ x < P.length)
    (hok : ord.Pairwise fun a b => a < b ∨ ((toBundle P).conflict a b = false ∨ (a, b) ∈ extra)) :
    ord.foldl (fun s j => cstep P fuel j s) (G0 P, []) =
      (List.range P.length).foldl (fun s j => cstep P fuel j s) (G0 P, []) := by
  refine reorder_gen (fun j s => cstep P fuel j s)
    (fun a b => (toBundle P).conflict a b = false ∨ (a, b) ∈ extra) (fun s => Inv P s.1)
    (fun j s hs => ?_) P.length ?_ (G0 P, []) (inv_G0 hwf) P.length (Nat.le_refl _) ord hnd hmem hok
  · by_cases hj : j < P.length
    · exact cstep_inv hc fuel hj hs s.2
    · have hn : P[j]? = none := List.getElem?_eq_none (by omega)
      simpa only [cstep, hn] using hs
  · intro i j s hi hj hs hcm
    rcases hcm with h | h
    · exact cstep_comm_auto hc fuel hi hj h s hs
    · exact hextra _ h hi hj s hs

end MJS
