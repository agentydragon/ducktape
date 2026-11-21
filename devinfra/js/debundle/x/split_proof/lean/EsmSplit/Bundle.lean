import Std.Data.TreeMap
import Std.Data.TreeSet

/-!
# What we know about a bundle

A scope-hoisted bundle is one flat module scope: a list of top-level statements.
All the splitter and the proof need from each statement is its *footprint*
(what it declares, what it reads, which names it may assign, whether it has effects), plus its source text,
which is copied verbatim and never inspected.

Footprints come from the JavaScript front end (`extract/extract.mjs`, or `Experiments/Lower.lean`).
That front end is the trusted boundary; everything below it is checked by Lean.

Every check below is stated declaratively (`∀ … ∈ …`) and decided by an indexed algorithm
that is proved equal to the statement. The kernel and compiled code run the same algorithm.
-/

namespace EsmSplit

open Std (compare_eq_iff_eq compare_self)

abbrev Name := String
abbrev ModName := String
abbrev NameSet := Std.TreeSet Name
/-- For a declared name, everything its declarations mention. -/
abbrev Index := Std.TreeMap Name (List Name)

/-- One top-level statement of the bundle. -/
structure Stmt where
  /-- Verbatim JavaScript. Moved between modules unchanged. -/
  src     : String
  /-- Top-level bindings the statement declares. -/
  defs    : List Name := []
  /-- Top-level names (or globals) read while the statement itself runs. -/
  eager   : List Name := []
  /-- Names read only inside function bodies created here, i.e. when they are called later. -/
  lazy    : List Name := []
  /-- Names the statement may also read when it runs, beyond the ones it mentions: a function it calls, which it
      reached through an object another statement filled, mentions them. They order the statement like `eager` does,
      but the statement's text does not use them, so a module need not import them (`Hygienic` looks at `eager`
      and `lazy`). -/
  deps    : List Name := []
  /-- Names, declared elsewhere, that running the statement may assign: reassigned variables and the
      objects whose contents it changes. It lists every such name, including the ones a function it
      calls assigns (unknown code lists them all). Reading such a name is mentioning it in `eager`. -/
  writes  : List Name := []
  /-- Names, declared elsewhere, that the code of this statement assigns: while it runs, or in the function
      bodies it creates. Where this code sits matters for linking: an `import` is read-only. -/
  assigns : List Name := []
  /-- A `function` declaration: its binding exists before any code runs. -/
  hoisted : Bool := false
  /-- Touches the world: the environment, or code that is not known (a call into a library, a function it was handed).
      Effectful statements keep their relative order. A statement that only builds and changes objects of the program
      is not an effect: its `writes` order it against whoever shares an object with it. -/
  effect  : Bool := false
  deriving Repr, Inhabited

/-- What a statement binds: the names it declares and the names it assigns. -/
def Stmt.binds (s : Stmt) : List Name := s.defs ++ s.writes

abbrev Bundle := List Stmt

def Bundle.stmt (B : Bundle) (i : Nat) : Stmt := B[i]?.getD default

/-- Loops over statements look them up in this array, not in the list. -/
theorem Bundle.toArray_getD (B : Bundle) (i : Nat) : B.toArray.getD i default = B.stmt i := by
  simp [Bundle.stmt, Array.getD_eq_getD_getElem?]

/-- Statements that declare `u`. -/
def Bundle.declarers (B : Bundle) (u : Name) : List Stmt := B.filter (·.defs.contains u)

/-- First statement declaring `u`; `none` for globals such as `console`. -/
def Bundle.ownerOf (B : Bundle) (u : Name) : Option Nat := B.findIdx? (·.defs.contains u)

/-! ## The read closure

Running a statement may call any function value it can read, and a function body reads its `lazy`
names. So what a statement may read is its `eager` and `deps` names plus, transitively, whatever the
declarations of those names mention. -/

/-- A set of names (as a predicate) is closed: a name in it brings in everything its declarations mention. -/
def Bundle.Closed (B : Bundle) (P : Name → Prop) : Prop :=
  ∀ u, P u → ∀ s ∈ B.declarers u, ∀ w ∈ s.eager ++ s.lazy, P w

/-- `u ↦ everything the declarations of `u` mention`, built in one pass. -/
def Bundle.index (B : Bundle) : Index :=
  B.foldl (fun idx s => s.defs.foldl (fun idx d => idx.insert d (idx.getD d [] ++ (s.eager ++ s.lazy))) idx) ∅

private theorem foldl_defs (ms : List Name) : ∀ (defs : List Name) (idx : Index) (u w : Name),
    w ∈ (defs.foldl (fun idx d => idx.insert d (idx.getD d [] ++ ms)) idx).getD u [] ↔
      w ∈ idx.getD u [] ∨ (u ∈ defs ∧ w ∈ ms)
  | [], idx, u, w => by simp
  | d :: ds, idx, u, w => by
    rw [List.foldl_cons, foldl_defs ms ds, Std.TreeMap.getD_insert]
    by_cases h : d = u
    · subst h
      simp only [compare_self, ↓reduceIte, List.mem_append, List.mem_cons, true_or, true_and]
      constructor
      · rintro ((h | h) | ⟨_, h⟩)
        · exact Or.inl h
        · exact Or.inr h
        · exact Or.inr h
      · rintro (h | h)
        · exact Or.inl (Or.inl h)
        · exact Or.inl (Or.inr h)
    · have hc : compare d u ≠ .eq := fun e => h (compare_eq_iff_eq.1 e)
      simp only [hc, ↓reduceIte, List.mem_cons, Ne.symm h, false_or]

private theorem foldl_stmts : ∀ (L : List Stmt) (idx : Index) (u w : Name),
    w ∈ (L.foldl (fun idx s => s.defs.foldl
        (fun idx d => idx.insert d (idx.getD d [] ++ (s.eager ++ s.lazy))) idx) idx).getD u [] ↔
      w ∈ idx.getD u [] ∨ ∃ s ∈ L, u ∈ s.defs ∧ w ∈ s.eager ++ s.lazy
  | [], idx, u, w => by simp
  | s :: L, idx, u, w => by
    rw [List.foldl_cons, foldl_stmts L, foldl_defs]
    simp only [List.mem_cons]
    constructor
    · rintro ((h | h) | ⟨s', hs', h⟩)
      · exact Or.inl h
      · exact Or.inr ⟨s, Or.inl rfl, h⟩
      · exact Or.inr ⟨s', Or.inr hs', h⟩
    · rintro (h | ⟨s', hs' | hs', h⟩)
      · exact Or.inl (Or.inl h)
      · subst hs'; exact Or.inl (Or.inr h)
      · exact Or.inr ⟨s', hs', h⟩

theorem Bundle.mem_index (B : Bundle) (u w : Name) :
    w ∈ B.index.getD u [] ↔ ∃ s ∈ B.declarers u, w ∈ s.eager ++ s.lazy := by
  simp only [Bundle.index, foldl_stmts, Std.TreeMap.getD_emptyc, List.not_mem_nil, false_or,
    Bundle.declarers, List.mem_filter, List.contains_iff_mem]
  constructor
  · rintro ⟨s, hs, hu, hw⟩; exact ⟨s, ⟨hs, hu⟩, hw⟩
  · rintro ⟨s, ⟨hs, hu⟩, hw⟩; exact ⟨s, hs, hu, hw⟩

theorem mem_insert_iff {α : Type} [Ord α] [Std.TransOrd α] [Std.LawfulEqOrd α] {s : Std.TreeSet α} {u x : α} :
    x ∈ s.insert u ↔ x = u ∨ x ∈ s := by
  rw [Std.TreeSet.mem_insert]
  constructor
  · rintro (h | h)
    · exact Or.inl (compare_eq_iff_eq.1 h).symm
    · exact Or.inr h
  · rintro (h | h)
    · subst h; exact Or.inl compare_self
    · exact Or.inr h

/-- Everything reachable from `work` through the index, added to `seen`.
`none` if `fuel` ran out with work left; `Bundle.fuel` is more than any run needs. -/
def closeFrom (idx : Index) : Nat → List Name → NameSet → Option NameSet
  | 0, work, seen => if work.isEmpty then some seen else none
  | _ + 1, [], seen => some seen
  | fuel + 1, u :: work, seen =>
    if seen.contains u then closeFrom idx fuel work seen
    else closeFrom idx fuel (idx.getD u [] ++ work) (seen.insert u)

/-- What `closeFrom` leaves behind, for every starting point:
it keeps what it was given, it is closed provided nothing was left dangling, and it is the least such set. -/
theorem closeFrom_spec (idx : Index) : ∀ (fuel : Nat) (work : List Name) (seen R : NameSet),
    closeFrom idx fuel work seen = some R →
      (∀ x ∈ seen, x ∈ R) ∧ (∀ x ∈ work, x ∈ R)
      ∧ ((∀ u ∈ seen, ∀ w ∈ idx.getD u [], w ∈ seen ∨ w ∈ work) →
          ∀ u ∈ R, ∀ w ∈ idx.getD u [], w ∈ R)
      ∧ (∀ S : Name → Prop, (∀ u, S u → ∀ w ∈ idx.getD u [], S w) →
          (∀ x ∈ seen, S x) → (∀ x ∈ work, S x) → ∀ x ∈ R, S x) := by
  intro fuel
  induction fuel with
  | zero =>
    intro work seen R h
    simp only [closeFrom] at h
    split at h
    · cases work with
      | nil =>
        cases h
        refine ⟨fun x hx => hx, by simp, fun hI u hu w hw => ?_, fun S _ hs _ x hx => hs x hx⟩
        simpa using hI u hu w hw
      | cons a l => simp at *
    · cases h
  | succ fuel ih =>
    intro work seen R h
    cases work with
    | nil =>
      simp only [closeFrom, Option.some.injEq] at h
      subst h
      refine ⟨fun x hx => hx, by simp, fun hI u hu w hw => ?_, fun S _ hs _ x hx => hs x hx⟩
      simpa using hI u hu w hw
    | cons u work =>
      simp only [closeFrom] at h
      by_cases hu : seen.contains u = true
      · simp only [hu, ↓reduceIte] at h
        obtain ⟨h1, h2, h3, h4⟩ := ih work seen R h
        have hus : u ∈ seen := Std.TreeSet.mem_iff_contains.2 hu
        refine ⟨h1, fun x hx => ?_, fun hI => h3 fun v hv w hw => ?_, fun S hS hs hw => h4 S hS hs fun x hx => hw x (List.mem_cons_of_mem _ hx)⟩
        · rcases List.mem_cons.1 hx with rfl | hx
          · exact h1 _ hus
          · exact h2 x hx
        · rcases hI v hv w hw with h | h
          · exact Or.inl h
          · rcases List.mem_cons.1 h with rfl | h
            · exact Or.inl hus
            · exact Or.inr h
      · simp only [hu] at h
        obtain ⟨h1, h2, h3, h4⟩ := ih _ _ R h
        refine ⟨fun x hx => h1 x (mem_insert_iff.2 (Or.inr hx)), fun x hx => ?_, fun hI => h3 fun v hv w hw => ?_, fun S hS hs hw => ?_⟩
        · rcases List.mem_cons.1 hx with rfl | hx
          · exact h1 _ (mem_insert_iff.2 (Or.inl rfl))
          · exact h2 x (List.mem_append_right _ hx)
        · rcases mem_insert_iff.1 hv with rfl | hv
          · exact Or.inr (List.mem_append_left _ hw)
          · rcases hI v hv w hw with h | h
            · exact Or.inl (mem_insert_iff.2 (Or.inr h))
            · rcases List.mem_cons.1 h with rfl | h
              · exact Or.inl (mem_insert_iff.2 (Or.inl rfl))
              · exact Or.inr (List.mem_append_right _ h)
        · have hSu : S u := hw u (List.mem_cons_self ..)
          refine h4 S hS (fun x hx => ?_) (fun x hx => ?_)
          · rcases mem_insert_iff.1 hx with rfl | hx
            · exact hSu
            · exact hs x hx
          · rcases List.mem_append.1 hx with hx | hx
            · exact hS u hSu x hx
            · exact hw x (List.mem_cons_of_mem _ hx)

/-- More fuel than `closeFrom` can use: one step per name pushed, and each declared name pushes at most once. -/
def Bundle.fuel (B : Bundle) : Nat :=
  1 + (B.map fun s => (s.defs.length + 1) * (s.eager.length + s.lazy.length + 1) + s.deps.length).sum

/-- Everything running statement `i` may read, given the index of `B`. A hoisted declaration reads nothing when run.
`none` if the fuel ran out. -/
def Bundle.readsSet (B : Bundle) (idx : Index) (i : Nat) : Option NameSet :=
  let s := B.stmt i
  if s.hoisted then some ∅ else closeFrom idx B.fuel (s.eager ++ s.deps) ∅

/-- `readsSet` with the statements, the index and the fuel passed in, so a loop builds them once. -/
def readsSetWith (arr : Array Stmt) (idx : Index) (fuel : Nat) (i : Nat) : Option NameSet :=
  let s := arr.getD i default
  if s.hoisted then some ∅ else closeFrom idx fuel (s.eager ++ s.deps) ∅

theorem readsSetWith_eq (B : Bundle) (idx : Index) (i : Nat) :
    readsSetWith B.toArray idx B.fuel i = B.readsSet idx i := by
  simp only [readsSetWith, Bundle.readsSet, Bundle.toArray_getD]

/-- The reads of `i` (empty if the fuel ran out, which `ReadsClosed` rejects). Recomputes the index; checks use `readsSetWith`. -/
def Bundle.readsOf (B : Bundle) (i : Nat) : NameSet := (B.readsSet B.index i).getD ∅

def Bundle.reads (B : Bundle) (i : Nat) : List Name := (B.readsOf i).toList

theorem Bundle.mem_reads (B : Bundle) (i : Nat) (x : Name) : x ∈ B.reads i ↔ x ∈ B.readsOf i :=
  Std.TreeSet.mem_toList

/-- The closure ran to completion for every statement. Everything else about `reads` is a theorem. -/
def Bundle.ReadsDefined (B : Bundle) (arr : Array Stmt) (idx : Index) (fuel : Nat) : Prop :=
  ∀ i ∈ List.range B.length, (readsSetWith arr idx fuel i).isSome = true

def ReadsClosed (B : Bundle) : Prop := B.ReadsDefined B.toArray B.index B.fuel

instance (B : Bundle) (arr : Array Stmt) (idx : Index) (fuel : Nat) : Decidable (B.ReadsDefined arr idx fuel) := by
  unfold Bundle.ReadsDefined; infer_instance
instance (B : Bundle) : Decidable (ReadsClosed B) := by unfold ReadsClosed; infer_instance

section reads
variable {B : Bundle} {i : Nat}

theorem Bundle.reads_super (hc : (B.readsSet B.index i).isSome = true) (hh : (B.stmt i).hoisted = false) :
    ∀ w ∈ (B.stmt i).eager ++ (B.stmt i).deps, w ∈ B.reads i := by
  intro w hw
  obtain ⟨R, hR⟩ := Option.isSome_iff_exists.1 hc
  simp only [Bundle.readsSet, hh, Bool.false_eq_true, ↓reduceIte] at hR
  have := (closeFrom_spec _ _ _ _ _ hR).2.1 w hw
  rw [Bundle.mem_reads]
  simpa [Bundle.readsOf, Bundle.readsSet, hh, hR] using this

theorem Bundle.reads_closed (hc : (B.readsSet B.index i).isSome = true) :
    B.Closed (· ∈ B.reads i) := by
  obtain ⟨R, hR⟩ := Option.isSome_iff_exists.1 hc
  intro u hu s hs w hw
  have hmem : ∀ x, x ∈ B.reads i ↔ x ∈ R := fun x => by
    rw [Bundle.mem_reads]; simp [Bundle.readsOf, hR]
  rw [hmem] at hu ⊢
  by_cases hh : (B.stmt i).hoisted = true
  · simp [Bundle.readsSet, hh] at hR
    subst hR; exact absurd hu (Std.TreeSet.not_mem_emptyc)
  · simp only [Bundle.readsSet, hh, Bool.false_eq_true, ↓reduceIte] at hR
    refine (closeFrom_spec _ _ _ _ _ hR).2.2.1 (fun v hv => ?_) u hu w ?_
    · simp at hv
    · exact (B.mem_index u w).2 ⟨s, hs, hw⟩

theorem Bundle.reads_least {P : Name → Prop} (hP : B.Closed P)
    (h : (B.stmt i).hoisted = false → ∀ w ∈ (B.stmt i).eager ++ (B.stmt i).deps, P w) : ∀ w ∈ B.reads i, P w := by
  intro w hw
  rw [Bundle.mem_reads] at hw
  by_cases hs : (B.readsSet B.index i).isSome = true
  · obtain ⟨R, hR⟩ := Option.isSome_iff_exists.1 hs
    have hw' : w ∈ R := by simpa [Bundle.readsOf, hR] using hw
    by_cases hh : (B.stmt i).hoisted = true
    · simp [Bundle.readsSet, hh] at hR; subst hR; exact absurd hw' Std.TreeSet.not_mem_emptyc
    · simp only [Bundle.readsSet, hh, Bool.false_eq_true, ↓reduceIte] at hR
      have hh' : (B.stmt i).hoisted = false := by simpa using hh
      refine (closeFrom_spec _ _ _ _ _ hR).2.2.2 P ?_ (fun x hx => by simp at hx) (h hh') w hw'
      intro u hu v hv
      obtain ⟨s, hs, hv'⟩ := (B.mem_index u v).1 hv
      exact hP u hu s hs v hv'
  · have : B.readsSet B.index i = none := by simpa using hs
    simp [Bundle.readsOf, this] at hw

end reads

/-! ## Conflicts -/

/-- Statements `i` and `j` must keep their relative order if they conflict:
both have effects, one reads a name the other binds (declares or assigns), or both bind the same name.
A hoisted declaration is available from the start, so nobody waits for it, and running it changes nothing. -/
def Bundle.conflict (B : Bundle) (i j : Nat) : Bool :=
  let a := B.stmt i
  let b := B.stmt j
  (a.effect && b.effect)
    || (!b.hoisted && b.binds.any (B.readsOf i).contains)
    || (!a.hoisted && a.binds.any (B.readsOf j).contains)
    || (!a.hoisted && !b.hoisted && a.binds.any b.binds.contains)

/-- Conflict is independent of argument order, including for hoisted declarations. -/
theorem Bundle.conflict_symm (B : Bundle) (i j : Nat) : B.conflict i j = B.conflict j i := by
  have overlap_symm (xs ys : List Name) : xs.any ys.contains = ys.any xs.contains := by
    apply Bool.eq_iff_iff.mpr
    simp only [List.any_eq_true, List.contains_iff_mem]
    constructor <;> rintro ⟨x, hx, hy⟩ <;> exact ⟨x, hy, hx⟩
  simp only [Bundle.conflict, overlap_symm, Bool.and_comm, Bool.and_assoc, Bool.or_comm, Bool.or_left_comm,
    Bool.or_assoc]

/-- Statements and read sets of all statements, as arrays, for the checks that look them up many times. -/
def Bundle.readsTab (B : Bundle) : Array NameSet :=
  let arr := B.toArray
  let idx := B.index
  let fuel := B.fuel
  ((List.range B.length).map fun i => (readsSetWith arr idx fuel i).getD ∅).toArray

/-- `conflict` with the statements and read sets looked up in tables built once. -/
def Bundle.conflictT (arr : Array Stmt) (tab : Array NameSet) (i j : Nat) : Bool :=
  let a := arr.getD i default
  let b := arr.getD j default
  (a.effect && b.effect)
    || (!b.hoisted && b.binds.any (tab.getD i ∅).contains)
    || (!a.hoisted && a.binds.any (tab.getD j ∅).contains)
    || (!a.hoisted && !b.hoisted && a.binds.any b.binds.contains)

theorem Bundle.readsTab_getD (B : Bundle) (i : Nat) : B.readsTab.getD i ∅ = B.readsOf i := by
  by_cases hi : i < B.length
  · simp [Bundle.readsTab, Bundle.readsOf, hi, readsSetWith_eq]
  · have h1 : B.stmt i = default := by simp [Bundle.stmt, List.getElem?_eq_none (Nat.le_of_not_lt hi)]
    have h2 : B.readsOf i = ∅ := by
      have : B.readsSet B.index i = some ∅ := by
        have hh : (default : Stmt).hoisted = false := rfl
        have he : (default : Stmt).eager = [] := rfl
        have hd : (default : Stmt).deps = [] := rfl
        cases hf : B.fuel with
        | zero => simp [Bundle.readsSet, h1, hh, he, hd, hf, closeFrom]
        | succ n => simp [Bundle.readsSet, h1, hh, he, hd, hf, closeFrom]
      simp [Bundle.readsOf, this]
    rw [h2]; simp [Bundle.readsTab, hi]

theorem Bundle.conflictT_eq (B : Bundle) (i j : Nat) :
    Bundle.conflictT B.toArray B.readsTab i j = B.conflict i j := by
  simp only [Bundle.conflictT, Bundle.conflict, Bundle.toArray_getD, Bundle.readsTab_getD]

/-! ## One flat scope: no two statements declare the same name -/

def UniqueDefs (B : Bundle) : Prop := B.Pairwise fun a b => ∀ u ∈ a.defs, u ∉ b.defs

/-- All names declared by `L`, if no two statements of `L` declare the same name. -/
def uniqueDefsOf : List Stmt → Option NameSet
  | [] => some ∅
  | s :: rest => (uniqueDefsOf rest).bind fun seen =>
      if s.defs.any seen.contains then none else some (s.defs.foldl (·.insert ·) seen)

theorem mem_foldl_insert {α : Type} [Ord α] [Std.TransOrd α] [Std.LawfulEqOrd α] :
    ∀ (l : List α) (seen : Std.TreeSet α) (x : α),
    x ∈ l.foldl (·.insert ·) seen ↔ x ∈ seen ∨ x ∈ l
  | [], seen, x => by simp
  | a :: l, seen, x => by
    rw [List.foldl_cons, mem_foldl_insert l, mem_insert_iff]
    simp only [List.mem_cons]
    constructor
    · rintro ((h | h) | h)
      · exact Or.inr (Or.inl h)
      · exact Or.inl h
      · exact Or.inr (Or.inr h)
    · rintro (h | h | h)
      · exact Or.inl (Or.inr h)
      · exact Or.inl (Or.inl h)
      · exact Or.inr h

private theorem uniqueDefsOf_spec : ∀ (L : List Stmt) (seen : NameSet), uniqueDefsOf L = some seen →
    (∀ u, u ∈ seen ↔ ∃ s ∈ L, u ∈ s.defs) ∧ L.Pairwise fun a b => ∀ u ∈ a.defs, u ∉ b.defs
  | [], seen, h => by
    simp only [uniqueDefsOf, Option.some.injEq] at h
    subst h; simp
  | s :: rest, seen, h => by
    simp only [uniqueDefsOf, Option.bind_eq_some_iff] at h
    obtain ⟨seen0, h0, h1⟩ := h
    obtain ⟨hmem, hpw⟩ := uniqueDefsOf_spec rest seen0 h0
    by_cases hany : s.defs.any seen0.contains = true
    · simp [hany] at h1
    · simp only [hany, Bool.false_eq_true, ↓reduceIte, Option.some.injEq] at h1
      subst h1
      refine ⟨fun u => ?_, List.pairwise_cons.2 ⟨fun b hb u hu hub => ?_, hpw⟩⟩
      · simp only [mem_foldl_insert, hmem, List.mem_cons]
        constructor
        · rintro (⟨t, ht, h⟩ | h)
          · exact ⟨t, Or.inr ht, h⟩
          · exact ⟨s, Or.inl rfl, h⟩
        · rintro ⟨t, rfl | ht, h⟩
          · exact Or.inr h
          · exact Or.inl ⟨t, ht, h⟩
      · have : seen0.contains u = true := Std.TreeSet.mem_iff_contains.1 ((hmem u).2 ⟨b, hb, hub⟩)
        simp only [Bool.not_eq_true, List.any_eq_false] at hany
        rw [hany u hu] at this
        exact Bool.noConfusion this

private theorem uniqueDefsOf_complete : ∀ L : List Stmt,
    (L.Pairwise fun a b => ∀ u ∈ a.defs, u ∉ b.defs) → ∃ seen, uniqueDefsOf L = some seen
  | [], _ => ⟨_, rfl⟩
  | s :: rest, h => by
    obtain ⟨hs, hpw⟩ := List.pairwise_cons.1 h
    obtain ⟨seen0, h0⟩ := uniqueDefsOf_complete rest hpw
    obtain ⟨hmem, _⟩ := uniqueDefsOf_spec rest seen0 h0
    have : s.defs.any seen0.contains = false := by
      simp only [List.any_eq_false, Bool.not_eq_true]
      intro u hu
      cases hc : seen0.contains u with
      | false => rfl
      | true =>
        obtain ⟨t, ht, hut⟩ := (hmem u).1 (Std.TreeSet.mem_iff_contains.2 hc)
        exact absurd hut (hs t ht u hu)
    refine ⟨s.defs.foldl (·.insert ·) seen0, ?_⟩
    simp only [uniqueDefsOf, h0, Option.bind_some, this, Bool.false_eq_true, ↓reduceIte]

theorem uniqueDefsOf_iff (B : Bundle) : (uniqueDefsOf B).isSome = true ↔ UniqueDefs B := by
  constructor
  · intro h
    obtain ⟨seen, hs⟩ := Option.isSome_iff_exists.1 h
    exact (uniqueDefsOf_spec B seen hs).2
  · intro h
    obtain ⟨seen, hs⟩ := uniqueDefsOf_complete B h
    simp [hs]

instance (B : Bundle) : Decidable (UniqueDefs B) := decidable_of_iff _ (uniqueDefsOf_iff B)

end EsmSplit
