import EsmSplit.Plan

/-!
# Deciding the checks fast

`Plan.lean` states each check as a plain `∀ … ∈ …`. Evaluating those literally scans lists inside
loops. Here each check gets an algorithm that looks things up in `TreeSet`/`TreeMap` tables built
once, and a theorem that it computes the same thing as the statement. `decide` (compiled) and
`decide +kernel` both run these algorithms through the `Decidable` instances at the bottom.
-/

namespace EsmSplit

open Std (compare_eq_iff_eq compare_self)

/-! ## Duplicates, found with a set -/

section generic
variable {α : Type} [Ord α]

def nodupFrom (seen : Std.TreeSet α) : List α → Bool
  | [] => true
  | x :: l => !seen.contains x && nodupFrom (seen.insert x) l

theorem contains_false_iff {s : Std.TreeSet α} {x : α} : s.contains x = false ↔ x ∉ s := by
  constructor
  · intro h hm
    have := Std.TreeSet.mem_iff_contains.1 hm
    rw [h] at this; exact Bool.noConfusion this
  · intro h
    cases hc : s.contains x
    · rfl
    · exact absurd (Std.TreeSet.mem_iff_contains.2 hc) h

variable [Std.TransOrd α] [Std.LawfulEqOrd α]

theorem nodupFrom_iff : ∀ (l : List α) (seen : Std.TreeSet α),
    nodupFrom seen l = true ↔ l.Nodup ∧ ∀ x ∈ l, x ∉ seen
  | [], seen => by simp [nodupFrom]
  | x :: l, seen => by
    rw [nodupFrom, Bool.and_eq_true, nodupFrom_iff l (seen.insert x), List.nodup_cons]
    simp only [Bool.not_eq_true', contains_false_iff, mem_insert_iff, List.mem_cons]
    constructor
    · rintro ⟨hx, ⟨hnd, h2⟩⟩
      exact ⟨⟨fun hm => (h2 x hm (Or.inl rfl)), hnd⟩, fun y hy => by
        rcases hy with rfl | hy
        · exact hx
        · exact fun hs => h2 y hy (Or.inr hs)⟩
    · rintro ⟨⟨hxl, hnd⟩, h2⟩
      refine ⟨h2 x (Or.inl rfl), hnd, fun y hy hs => ?_⟩
      rcases hs with rfl | hs
      · exact hxl hy
      · exact h2 y (Or.inr hy) hs

end generic

def nodupB (l : List Name) : Bool := nodupFrom ∅ l

theorem nodupB_iff (l : List Name) : nodupB l = true ↔ l.Nodup := by
  rw [nodupB, nodupFrom_iff]; simp


/-! ## Permutes, with a set -/

def permutesB (n : Nat) (ord : List Nat) : Bool :=
  nodupFrom (∅ : Std.TreeSet Nat) ord && ord.all (· < n) &&
    (List.range n).all (ord.foldl (·.insert ·) (∅ : Std.TreeSet Nat)).contains

theorem permutesB_iff (n : Nat) (ord : List Nat) :
    permutesB n ord = true ↔ ord.Nodup ∧ (∀ x ∈ ord, x < n) ∧ ∀ x ∈ List.range n, x ∈ ord := by
  rw [permutesB, Bool.and_eq_true, Bool.and_eq_true, nodupFrom_iff]
  simp only [List.all_eq_true, decide_eq_true_eq, ← Std.TreeSet.mem_iff_contains, mem_foldl_insert,
    Std.TreeSet.not_mem_emptyc, false_or, not_false_eq_true, implies_true, and_true,
    and_assoc]

/-! ## Module tables

`Hygienic` and `Linkable` ask, for each name a statement mentions or a module imports, what the
module declares, exports and imports. Scanning the module for each answer is quadratic in its size.
`Module.tab` answers all three with lookups. -/

structure ModTab where
  declared : NameSet
  exported : NameSet
  /-- the module that the first `import` listing the name comes from -/
  importedFrom : Std.TreeMap Name ModName

def Module.tab (arr : Array Stmt) (m : Module) : ModTab where
  declared := m.stmts.foldl (fun acc i => (arr.getD i default).defs.foldl (·.insert ·) acc) ∅
  exported := m.exports.foldl (·.insert ·) ∅
  importedFrom := m.imports.foldr (fun e acc => e.2.foldr (fun u acc => acc.insert u e.1) acc) ∅

private theorem mem_foldl_decl (arr : Array Stmt) : ∀ (l : List Nat) (acc : NameSet) (u : Name),
    u ∈ l.foldl (fun acc i => (arr.getD i default).defs.foldl (·.insert ·) acc) acc ↔
      u ∈ acc ∨ ∃ i ∈ l, u ∈ (arr.getD i default).defs
  | [], acc, u => by simp
  | i :: l, acc, u => by
    rw [List.foldl_cons, mem_foldl_decl arr l, mem_foldl_insert]
    simp only [List.mem_cons]
    constructor
    · rintro ((h | h) | ⟨j, hj, h⟩)
      · exact Or.inl h
      · exact Or.inr ⟨i, Or.inl rfl, h⟩
      · exact Or.inr ⟨j, Or.inr hj, h⟩
    · rintro (h | ⟨j, rfl | hj, h⟩)
      · exact Or.inl (Or.inl h)
      · exact Or.inl (Or.inr h)
      · exact Or.inr ⟨j, hj, h⟩

theorem Module.tab_declared (B : Bundle) (m : Module) (u : Name) :
    (m.tab B.toArray).declared.contains u = m.declares B u := by
  rw [Bool.eq_iff_iff, ← Std.TreeSet.mem_iff_contains]
  simp only [Module.tab]
  rw [mem_foldl_decl]
  simp only [Std.TreeSet.not_mem_emptyc, false_or, Module.declares, List.any_eq_true,
    List.contains_iff_mem, Bundle.toArray_getD]

theorem Module.tab_exported (B : Bundle) (m : Module) (u : Name) :
    (m.tab B.toArray).exported.contains u = m.exports.contains u := by
  rw [Bool.eq_iff_iff, ← Std.TreeSet.mem_iff_contains]
  simp only [Module.tab, mem_foldl_insert, Std.TreeSet.not_mem_emptyc, false_or, List.contains_iff_mem]

private theorem foldr_insert_get (src : ModName) : ∀ (names : List Name) (acc : Std.TreeMap Name ModName) (u : Name),
    (names.foldr (fun u acc => acc.insert u src) acc)[u]? = if u ∈ names then some src else acc[u]?
  | [], acc, u => by simp
  | d :: ds, acc, u => by
    rw [List.foldr_cons, Std.TreeMap.getElem?_insert, foldr_insert_get src ds]
    by_cases h : d = u
    · subst h; simp [compare_self]
    · have hc : compare d u ≠ .eq := fun e => h (compare_eq_iff_eq.1 e)
      simp [hc, Ne.symm h]

theorem Module.tab_importedFrom (arr : Array Stmt) (m : Module) (u : Name) :
    (m.tab arr).importedFrom[u]? = (m.imports.find? (·.2.contains u)).map (·.1) := by
  simp only [Module.tab]
  induction m.imports with
  | nil => simp
  | cons e rest ih =>
    rw [List.foldr_cons, foldr_insert_get e.1 e.2, ih, List.find?_cons]
    by_cases h : e.2.contains u = true
    · simp [List.contains_iff_mem.1 h]
    · have h' : u ∉ e.2 := fun hm => h (List.contains_iff_mem.2 hm)
      simp [h']


/-! ## Hygienic and Linkable, with tables -/

def Plan.tabs (arr : Array Stmt) (P : Plan) : List (Module × ModTab) := P.mods.map fun m => (m, m.tab arr)

/-- The table of the first module with each name (`Plan.find`). -/
def tabsByName (ts : List (Module × ModTab)) : Std.TreeMap ModName ModTab :=
  ts.foldr (fun e acc => acc.insert e.1.name e.2) ∅

theorem tabsByName_get : ∀ (ts : List (Module × ModTab)) (src : ModName),
    (tabsByName ts)[src]? = (ts.find? (·.1.name == src)).map (·.2)
  | [], src => by simp [tabsByName]
  | e :: rest, src => by
    rw [tabsByName, List.foldr_cons, Std.TreeMap.getElem?_insert]
    rw [← tabsByName, tabsByName_get rest src, List.find?_cons]
    by_cases h : e.1.name = src
    · simp [h, compare_self]
    · have hc : compare e.1.name src ≠ .eq := fun c => h (compare_eq_iff_eq.1 c)
      have hb : (e.1.name == src) = false := by simpa using h
      simp [hc, hb]

theorem Plan.tabsByName_get (B : Bundle) (P : Plan) (src : ModName) :
    (tabsByName (P.tabs B.toArray))[src]? = (P.find src).map (·.tab B.toArray) := by
  rw [_root_.EsmSplit.tabsByName_get, Plan.tabs, List.find?_map, Plan.find]
  simp [Function.comp_def, Option.map_map]

/-- `Plan.resolve`, with the tables. -/
def resolveT (byName : Std.TreeMap ModName ModTab) (m : Module) (t : ModTab) (u : Name) : Binding :=
  if t.declared.contains u then .declared m.name else
    match t.importedFrom[u]? with
    | none => .global
    | some src =>
      if (byName[src]?).any (fun s => s.declared.contains u && s.exported.contains u) then .declared src
      else .unbound

/-- `Plan.declaredIn`, with the tables. -/
def declaredInT (ts : List (Module × ModTab)) (u : Name) : Binding :=
  match ts.find? (·.2.declared.contains u) with
  | some e => .declared e.1.name
  | none => .global

theorem declaredInT_eq (B : Bundle) (P : Plan) (u : Name) :
    declaredInT (P.tabs B.toArray) u = P.declaredIn B u := by
  simp only [declaredInT, Plan.tabs, Plan.declaredIn, List.find?_map, Function.comp_def,
    Module.tab_declared]
  cases P.mods.find? (·.declares B u) <;> simp

private theorem any_tab (B : Bundle) (u : Name) (o : Option Module) :
    (o.map (·.tab B.toArray)).any (fun s => s.declared.contains u && s.exported.contains u) =
      (o.any (·.declares B u) && o.any (·.exports.contains u)) := by
  cases o <;> simp [Module.tab_declared, Module.tab_exported]

theorem resolveT_eq (B : Bundle) (P : Plan) (m : Module) (u : Name) :
    resolveT (tabsByName (P.tabs B.toArray)) m (m.tab B.toArray) u = P.resolve B m u := by
  simp only [resolveT, Plan.resolve, Module.tab_declared, Module.tab_importedFrom]
  by_cases hd : m.declares B u = true
  · simp [hd]
  · simp only [hd, Bool.false_eq_true, ↓reduceIte]
    cases hf : m.imports.find? (·.2.contains u) with
    | none => simp
    | some e =>
      simp only [Option.map_some, Plan.tabsByName_get, Plan.declaresIn, Plan.exportsName, any_tab]
      rfl

/-- `Hygienic`, with the tables. -/
def Plan.hygienicB (arr : Array Stmt) (P : Plan) : Bool :=
  let ts := P.tabs arr
  let byName := tabsByName ts
  ts.all fun e => e.1.stmts.all fun i =>
    let s := arr.getD i default
    (s.eager ++ s.lazy).all fun u => resolveT byName e.1 e.2 u == declaredInT ts u

theorem Plan.hygienicB_iff (B : Bundle) (P : Plan) : P.hygienicB B.toArray = true ↔ Hygienic B P := by
  simp only [Plan.hygienicB, Hygienic, List.all_eq_true, beq_iff_eq, Bundle.toArray_getD,
    declaredInT_eq]
  constructor
  · intro h m hm i hi u hu
    have := h (m, m.tab B.toArray) (List.mem_map.2 ⟨m, hm, rfl⟩) i hi u hu
    rwa [resolveT_eq] at this
  · rintro h ⟨m, t⟩ he i hi u hu
    obtain ⟨m', hm', hmm⟩ := List.mem_map.1 he
    obtain ⟨rfl, rfl⟩ := Prod.mk.inj hmm
    show resolveT _ m' (m'.tab B.toArray) u = _
    rw [resolveT_eq]; exact h m' hm' i hi u hu


/-- `Linkable` for one module, with the tables. -/
def linkModB (byName : Std.TreeMap ModName ModTab) (m : Module) (t : ModTab) : Bool :=
  m.imports.all (fun i => i.1 != m.name && (byName[i.1]?).isSome &&
      i.2.all fun u => (byName[i.1]?).any (·.exported.contains u))
    && nodupB (m.imports.map (·.1))
    && nodupB (m.imports.flatMap (·.2))
    && m.imports.all (fun i => i.2.all fun u => !t.declared.contains u)
    && nodupB m.exports
    && m.exports.all t.declared.contains

private theorem any_exported (B : Bundle) (u : Name) (o : Option Module) :
    (o.map (·.tab B.toArray)).any (·.exported.contains u) = o.any (·.exports.contains u) := by
  cases o <;> simp [Module.tab_exported]

theorem linkModB_iff (B : Bundle) (P : Plan) (m : Module) :
    linkModB (tabsByName (P.tabs B.toArray)) m (m.tab B.toArray) = true ↔
      ((∀ e ∈ m.imports, e.1 ≠ m.name ∧ (P.find e.1).isSome = true ∧ ∀ u ∈ e.2, P.exportsName e.1 u = true)
        ∧ (m.imports.map (·.1)).Nodup ∧ (m.imports.flatMap (·.2)).Nodup
        ∧ (∀ e ∈ m.imports, ∀ u ∈ e.2, m.declares B u = false)
        ∧ m.exports.Nodup ∧ (∀ u ∈ m.exports, m.declares B u = true)) := by
  simp only [linkModB, Bool.and_eq_true, List.all_eq_true, bne_iff_ne, ne_eq, nodupB_iff,
    Plan.tabsByName_get, Option.isSome_map, Module.tab_declared, Plan.exportsName, Bool.not_eq_true',
    any_exported, and_assoc]

def Plan.linkableB (arr : Array Stmt) (P : Plan) : Bool :=
  let ts := P.tabs arr
  let byName := tabsByName ts
  ts.all fun e => linkModB byName e.1 e.2

theorem Plan.linkableB_iff (B : Bundle) (P : Plan) : P.linkableB B.toArray = true ↔ Linkable B P := by
  simp only [Plan.linkableB, Linkable, List.all_eq_true]
  constructor
  · intro h m hm
    have := h (m, m.tab B.toArray) (List.mem_map.2 ⟨m, hm, rfl⟩)
    exact (linkModB_iff B P m).1 this
  · rintro h ⟨m, t⟩ he
    obtain ⟨m', hm', hmm⟩ := List.mem_map.1 he
    obtain ⟨rfl, rfl⟩ := Prod.mk.inj hmm
    exact (linkModB_iff B P m').2 (h m' hm')

/-! ## Faithful, with tables -/

def Selector.claimsT (arr : Array Stmt) (i : Nat) : Selector → Bool
  | .name u => (arr.getD i default).defs.contains u
  | .stmt k => k == i

theorem Selector.claimsT_eq (B : Bundle) (i : Nat) (sel : Selector) :
    sel.claimsT B.toArray i = sel.claims B i := by
  cases sel <;> simp [Selector.claimsT, Selector.claims, Bundle.stmt]

/-- `Spec.claimants`, reading statements from an array. -/
def Spec.claimantsT (S : Spec) (arr : Array Stmt) (i : Nat) : List ModName :=
  (S.modules.filter fun (_, sels) => sels.any (·.claimsT arr i)).map (·.1)

theorem Spec.claimantsT_eq (S : Spec) (B : Bundle) (i : Nat) :
    S.claimantsT B.toArray i = S.claimants B i := by
  simp only [Spec.claimantsT, Spec.claimants, Selector.claimsT_eq]

/-- Every name some statement declares. -/
def Bundle.allDefs (B : Bundle) : NameSet :=
  B.foldl (fun acc s => s.defs.foldl (·.insert ·) acc) ∅

private theorem mem_foldl_allDefs : ∀ (L : List Stmt) (acc : NameSet) (u : Name),
    u ∈ L.foldl (fun acc s => s.defs.foldl (·.insert ·) acc) acc ↔ u ∈ acc ∨ ∃ s ∈ L, u ∈ s.defs
  | [], acc, u => by simp
  | s :: L, acc, u => by
    rw [List.foldl_cons, mem_foldl_allDefs L, mem_foldl_insert]
    simp only [List.mem_cons]
    constructor
    · rintro ((h | h) | ⟨t, ht, h⟩)
      · exact Or.inl h
      · exact Or.inr ⟨s, Or.inl rfl, h⟩
      · exact Or.inr ⟨t, Or.inr ht, h⟩
    · rintro (h | ⟨t, rfl | ht, h⟩)
      · exact Or.inl (Or.inl h)
      · exact Or.inl (Or.inr h)
      · exact Or.inr ⟨t, ht, h⟩

theorem Bundle.mem_allDefs (B : Bundle) (u : Name) : u ∈ B.allDefs ↔ ∃ s ∈ B, u ∈ s.defs := by
  simp only [Bundle.allDefs, mem_foldl_allDefs, Std.TreeSet.not_mem_emptyc, false_or]

/-- Does the selector pick some statement of a bundle of `n` statements that declare `defs`? -/
def Selector.picksSomeT (n : Nat) (defs : NameSet) : Selector → Bool
  | .name u => defs.contains u
  | .stmt k => decide (k < n)

theorem Selector.picksSomeT_iff (B : Bundle) (sel : Selector) :
    sel.picksSomeT B.length B.allDefs = true ↔ ∃ i ∈ List.range B.length, sel.claims B i = true := by
  cases sel with
  | name u =>
    simp only [Selector.picksSomeT, ← Std.TreeSet.mem_iff_contains, Bundle.mem_allDefs,
      Selector.claims, List.contains_iff_mem, List.mem_range, Bundle.stmt]
    constructor
    · rintro ⟨s, hs, hu⟩
      obtain ⟨i, hi, rfl⟩ := List.getElem_of_mem hs
      exact ⟨i, hi, by simpa [hi] using hu⟩
    · rintro ⟨i, hi, hu⟩
      exact ⟨B[i], List.getElem_mem hi, by simpa [hi] using hu⟩
  | stmt k => simp [Selector.picksSomeT, Selector.claims]

/-- `Faithful`, with the claimants of each statement computed once and statement sets for the modules. -/
def faithfulB (B : Bundle) (arr : Array Stmt) (S : Spec) (P : Plan) : Bool :=
  let n := B.length
  let cl : List (Nat × List ModName) := (List.range n).map fun i => (i, S.claimantsT arr i)
  let allDefs := B.allDefs
  decide (P.entry = S.entry) && decide (P.mods.map (·.name) = S.modules.map (·.1))
    && nodupB (P.mods.map (·.name))
    && cl.all (fun e => e.2.length == 1)
    && P.mods.all (fun m =>
      let ms := m.stmts.foldl (·.insert ·) (∅ : Std.TreeSet Nat)
      cl.all fun e => ms.contains e.1 == decide (e.2 = [m.name]))
    && P.mods.all (fun m => m.stmts.all (· < n))
    && S.modules.all (fun e => e.2.all (·.picksSomeT n allDefs))

private theorem contains_foldl_insert (l : List Nat) (a : Nat) :
    (l.foldl (·.insert ·) (∅ : Std.TreeSet Nat)).contains a = decide (a ∈ l) := by
  rw [Bool.eq_iff_iff, ← Std.TreeSet.mem_iff_contains, mem_foldl_insert]
  simp

theorem faithfulB_iff (B : Bundle) (S : Spec) (P : Plan) :
    faithfulB B B.toArray S P = true ↔ Faithful B S P := by
  simp only [faithfulB, Faithful, Bool.and_eq_true, decide_eq_true_eq, nodupB_iff, List.all_eq_true,
    List.mem_map, beq_iff_eq, forall_exists_index, and_imp, forall_apply_eq_imp_iff₂,
    Spec.claimantsT_eq, Selector.picksSomeT_iff, and_assoc, contains_foldl_insert, decide_eq_decide]

/-! ## OrderOk, with tables built once -/

def orderOkB (arr : Array Stmt) (tab : Array NameSet) (ord : List Nat) : Bool :=
  decide (ord.Pairwise fun a b => a < b ∨ Bundle.conflictT arr tab a b = false)

/-! ## The instances -/

instance (B : Bundle) (S : Spec) (P : Plan) : Decidable (Faithful B S P) :=
  decidable_of_iff (faithfulB B B.toArray S P = true) (faithfulB_iff B S P)

instance (B : Bundle) (P : Plan) : Decidable (Linkable B P) :=
  decidable_of_iff _ (Plan.linkableB_iff B P)

instance (B : Bundle) (P : Plan) : Decidable (Hygienic B P) :=
  decidable_of_iff _ (Plan.hygienicB_iff B P)

instance (B : Bundle) (P : Plan) : Decidable (Permutes B P) :=
  decidable_of_iff (permutesB B.length P.execOrder = true) (permutesB_iff B.length P.execOrder)

instance (B : Bundle) (P : Plan) : Decidable (OrderOk B P) :=
  decidable_of_iff (orderOkB B.toArray B.readsTab P.execOrder = true) (by
    simp only [orderOkB, decide_eq_true_eq, OrderOk, Bundle.conflictT_eq])

instance (B : Bundle) (S : Spec) (P : Plan) : Decidable (Valid B S P) := by
  unfold Valid; infer_instance

end EsmSplit
