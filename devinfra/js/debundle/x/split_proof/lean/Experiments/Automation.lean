import EsmSplit.Semantics

open EsmSplit

set_option linter.unusedVariables false

/-!
# What Lean 4.35's own automation does with the fiddly lemmas

These are the list/commutation facts that took the most iterations to write by hand in
`Semantics.lean`. Each theorem below compiles. The comments say what `grind` could not do alone.
-/

/-- Shorten a permutation by its maximum: nodup and pairwise survive. `grind`, no help. -/
theorem shorten (n : Nat) (p q : List Nat) (R : Nat → Nat → Prop)
    (hnd : (p ++ n :: q).Nodup) (hok : (p ++ n :: q).Pairwise R) :
    (p ++ q).Nodup ∧ (p ++ q).Pairwise R := by
  grind

/-- Membership bookkeeping. Plain `grind` fails here (also with `List.mem_cons_self` as a hint);
`simp` to expose the structure, then `grind`, works. -/
theorem remove_max_members (n : Nat) (p q : List Nat) (hnd : (p ++ n :: q).Nodup)
    (hmem : ∀ x, x ∈ p ++ n :: q ↔ x < n + 1) : ∀ x, x ∈ p ++ q ↔ x < n := by
  have hn : n ∉ p ++ q := by
    intro h; simp [List.nodup_append] at hnd; simp at h; grind
  intro x; have := hmem x; simp at this ⊢; grind

/-- The moved element conflicts with nothing after it. Plain `grind` fails; unfolding
`Pairwise` and `Nodup` first, `grind` finishes. -/
theorem moved_commutes (n : Nat) (p q : List Nat) (c : Nat → Nat → Bool)
    (hmem : ∀ x, x ∈ p ++ n :: q ↔ x < n + 1) (hnd : (p ++ n :: q).Nodup)
    (hok : (p ++ n :: q).Pairwise (fun a b => a < b ∨ c a b = false)) :
    ∀ y ∈ q, c n y = false := by
  intro y hy
  simp only [List.pairwise_append, List.pairwise_cons] at hok
  simp only [List.nodup_append, List.nodup_cons] at hnd
  have := (hmem y).1 (by simp [hy])
  grind

/-- Commutation of non-conflicting statements. `grind [Bundle.step, Bundle.conflict]` alone fails.
With the case split and the two **frame facts** (`ej`, `ei`: updates to names a statement does not
read leave its inputs alone) written by hand, a generic `first | … | …` closes every case. The
frame facts are the idea; the rest is bookkeeping. -/
theorem step_comm_scaffolded {V W : Type} (B : Bundle) (M : Meaning V W) {i j : Nat}
    (h : B.conflict i j = false) (s : St V W) :
    B.step M j (B.step M i s) = B.step M i (B.step M j s) := by
  by_cases ha : (B.stmt i).hoisted = true
  · simp [Bundle.step, ha]
  by_cases hb : (B.stmt j).hoisted = true
  · simp [Bundle.step, hb]
  simp [Bundle.conflict, ha, hb, List.any_eq_false] at h
  obtain ⟨⟨⟨hE, hR1⟩, hR2⟩, hW⟩ := h
  have ej : ∀ (f : Name → V) (x : V), (B.reads j).map
      (fun u => if (B.stmt i).binds.contains u = true then x else f u) = (B.reads j).map f :=
    fun f x => List.map_congr_left fun u hu => by
      have : u ∉ (B.stmt i).binds := fun hd => hR2 u hd ((B.mem_reads j u).1 hu)
      simp [this]
  have ei : ∀ (f : Name → V) (x : V), (B.reads i).map
      (fun u => if (B.stmt j).binds.contains u = true then x else f u) = (B.reads i).map f :=
    fun f x => List.map_congr_left fun u hu => by
      have : u ∉ (B.stmt j).binds := fun hd => hR1 u hd ((B.mem_reads i u).1 hu)
      simp [this]
  by_cases hei : (B.stmt i).effect = true <;> by_cases hej : (B.stmt j).effect = true <;>
    simp only [Bundle.step, ha, hb, hei, hej, Bool.false_eq_true, ↓reduceIte, ej, ei] <;>
    first | (exfalso; grind) | (congr 1; funext u; grind)
