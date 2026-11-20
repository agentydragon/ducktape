import EsmSplit.Bundle

/-!
# "Equivalent-ish": same bindings, same final world, for every meaning of the statements

We do not formalize JavaScript. We run the statements of a bundle in *some order* over an
abstract state and ask what survives reordering.

* `V` is any type of values, `W` any type of "worlds" (heap contents, console output, …).
* A `Meaning` gives each statement a meaning that may depend only on what the footprint allows:
  - a statement without effects computes a value from the values of the names it may read
    (`B.reads j`, in that order);
  - a statement with effects also reads and changes the world.
* The state is the value of every name plus the world.
* Running `j` binds the names it declares and the names it assigns (`Stmt.binds`) to its value.
  A hoisted declaration is already bound; it does nothing.

`exec_reorder`: any order that keeps every conflicting pair in its original order
leaves the same final state. This is the whole content of "moving top-level statements between
modules does not change behavior", once the module system has produced the order.
-/

namespace EsmSplit

variable {V W : Type}

/-- What each statement means, constrained only by its footprint. -/
structure Meaning (V W : Type) where
  /-- a statement without effects: a function of the values it reads -/
  pure : Nat → List V → V
  /-- a statement with effects: also reads and changes the world; its value may depend on the world -/
  run  : Nat → List V → W → V × W

structure St (V W : Type) where
  env   : Name → V
  world : W

/-- Run statement `j`. -/
def Bundle.step (B : Bundle) (M : Meaning V W) (j : Nat) (s : St V W) : St V W :=
  if (B.stmt j).hoisted then s else
    let args := (B.reads j).map s.env
    if (B.stmt j).effect then
      let r := M.run j args s.world
      { env := fun u => if (B.stmt j).binds.contains u then r.1 else s.env u, world := r.2 }
    else
      let v := M.pure j args
      { env := fun u => if (B.stmt j).binds.contains u then v else s.env u, world := s.world }

/-- Run statements in the given order. -/
def Bundle.exec (B : Bundle) (M : Meaning V W) (s : St V W) (ord : List Nat) : St V W :=
  ord.foldl (fun s j => B.step M j s) s

theorem Bundle.step_hoisted (B : Bundle) (M : Meaning V W) {j : Nat}
    (h : (B.stmt j).hoisted = true) (s : St V W) : B.step M j s = s := by
  simp [Bundle.step, h]

/-- Two statements that do not conflict can run in either order. -/
theorem Bundle.step_comm (B : Bundle) (M : Meaning V W) {i j : Nat}
    (h : B.conflict i j = false) (s : St V W) :
    B.step M j (B.step M i s) = B.step M i (B.step M j s) := by
  by_cases ha : (B.stmt i).hoisted = true
  · rw [B.step_hoisted M ha, B.step_hoisted M ha]
  by_cases hb : (B.stmt j).hoisted = true
  · rw [B.step_hoisted M hb, B.step_hoisted M hb]
  simp only [Bool.not_eq_true] at ha hb
  simp [Bundle.conflict, ha, hb, List.any_eq_false] at h
  obtain ⟨⟨⟨⟨⟨hE, _, _⟩, hR1⟩, hR2⟩, hW⟩ := h
  have ej : ∀ (f : Name → V) (x : V), (B.reads j).map (fun u => if (B.stmt i).binds.contains u = true
        then x else f u) = (B.reads j).map f := fun f x =>
    List.map_congr_left fun u hu => by
      have : u ∉ (B.stmt i).binds := fun hd => hR2 u hd ((B.mem_reads j u).1 hu)
      simp [this]
  have ei : ∀ (f : Name → V) (x : V), (B.reads i).map (fun u => if (B.stmt j).binds.contains u = true
        then x else f u) = (B.reads i).map f := fun f x =>
    List.map_congr_left fun u hu => by
      have : u ∉ (B.stmt j).binds := fun hd => hR1 u hd ((B.mem_reads i u).1 hu)
      simp [this]
  have env_comm : ∀ (x y : V) (f : Name → V),
      (fun u => if (B.stmt j).binds.contains u = true then y else
        if (B.stmt i).binds.contains u = true then x else f u)
      = (fun u => if (B.stmt i).binds.contains u = true then x else
        if (B.stmt j).binds.contains u = true then y else f u) := by
    intro x y f
    funext u
    by_cases hu : u ∈ (B.stmt i).binds
    · simp [hu, hW u hu]
    · simp [hu]
  have hnot : ¬((B.stmt i).effect = true ∧ (B.stmt j).effect = true) :=
    fun ⟨a, b⟩ => by simp [hE a] at b
  by_cases hei : (B.stmt i).effect = true <;> by_cases hej : (B.stmt j).effect = true
  · exact absurd ⟨hei, hej⟩ hnot
  all_goals
    simp only [Bundle.step, ha, hb, hei, hej, Bool.false_eq_true, ↓reduceIte, ej, ei]
    congr 1
    exact env_comm _ _ _

theorem Bundle.exec_cons (B : Bundle) (M : Meaning V W) (s : St V W) (j : Nat) (ord : List Nat) :
    B.exec M s (j :: ord) = B.exec M (B.step M j s) ord := rfl

theorem Bundle.exec_append (B : Bundle) (M : Meaning V W) (s : St V W) (l l' : List Nat) :
    B.exec M s (l ++ l') = B.exec M (B.exec M s l) l' := List.foldl_append

/-- A statement that conflicts with nothing in `q` can be run after `q` instead of before it. -/
theorem Bundle.exec_move_past_by (B : Bundle) (M : Meaning V W) (conflict : Nat → Nat → Bool)
    (comm : ∀ i j, conflict i j = false → ∀ s, B.step M j (B.step M i s) = B.step M i (B.step M j s))
    {x : Nat} {q : List Nat} (hq : ∀ y ∈ q, conflict x y = false) (s : St V W) :
    B.exec M (B.step M x s) q = B.step M x (B.exec M s q) := by
  induction q generalizing s with
  | nil => rfl
  | cons y q ih =>
    rw [B.exec_cons, B.exec_cons, comm x y (hq y (List.mem_cons_self ..))]
    exact ih (fun z hz => hq z (List.mem_cons_of_mem _ hz)) _

theorem Bundle.exec_move_past (B : Bundle) (M : Meaning V W) {x : Nat} {q : List Nat}
    (hq : ∀ y ∈ q, B.conflict x y = false) (s : St V W) :
    B.exec M (B.step M x s) q = B.step M x (B.exec M s q) :=
  B.exec_move_past_by M B.conflict (fun _ _ h s => B.step_comm M h s) hq s

/-- **Reordering theorem.** Run the statements `0 … n-1` in any order that lists each exactly
once and keeps every conflicting pair in its original order. The final state (the value of every
name, and the world) is the same as when running them in bundle order, whatever the statements
mean. -/
theorem Bundle.exec_reorder_by (B : Bundle) (M : Meaning V W) (conflict : Nat → Nat → Bool)
    (comm : ∀ i j, conflict i j = false → ∀ s, B.step M j (B.step M i s) = B.step M i (B.step M j s))
    (s0 : St V W) :
    ∀ (n : Nat) (ord : List Nat), ord.Nodup → (∀ x, x ∈ ord ↔ x < n) →
      ord.Pairwise (fun a b => a < b ∨ conflict a b = false) →
      B.exec M s0 ord = B.exec M s0 (List.range n) := by
  intro n
  induction n with
  | zero =>
    intro ord _ hmem _
    cases ord with
    | nil => rfl
    | cons a _ => exact absurd ((hmem a).1 (List.mem_cons_self ..)) (Nat.not_lt_zero a)
  | succ n ih =>
    intro ord hnd hmem hok
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
    have hq : ∀ y ∈ q, conflict n y = false := by
      intro y hy
      rcases (List.pairwise_cons.1 hok'.2.1).1 y hy with h | h
      · exact absurd ((hpq y).1 (List.mem_append_right _ hy)) (Nat.not_lt.2 (Nat.le_of_lt h))
      · exact h
    have hperm : B.exec M s0 (p ++ n :: q) = B.step M n (B.exec M s0 (p ++ q)) := by
      rw [B.exec_append, B.exec_cons, B.exec_move_past_by M conflict comm hq, ← B.exec_append]
    rw [hperm, ih (p ++ q) ?_ hpq ?_, List.range_succ, B.exec_append]
    · rfl
    · exact List.nodup_append.2 ⟨hnd'.1, hnq.2, fun a ha b hb => hnd'.2.2 a ha b (List.mem_cons_of_mem _ hb)⟩
    · refine List.pairwise_append.2 ⟨hok'.1, (List.pairwise_cons.1 hok'.2.1).2, fun a ha b hb => ?_⟩
      exact hok'.2.2 a ha b (List.mem_cons_of_mem _ hb)

theorem Bundle.exec_reorder (B : Bundle) (M : Meaning V W) (s0 : St V W) :
    ∀ (n : Nat) (ord : List Nat), ord.Nodup → (∀ x, x ∈ ord ↔ x < n) →
      ord.Pairwise (fun a b => a < b ∨ B.conflict a b = false) →
      B.exec M s0 ord = B.exec M s0 (List.range n) :=
  B.exec_reorder_by M B.conflict (fun _ _ h s => B.step_comm M h s) s0

end EsmSplit
