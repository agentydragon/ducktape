import EsmSplit.CoreJS.PlanPlacement
import EsmSplit.CoreJS.Split
import EsmSplit.Split

/-! An order projection for source units whose original ordinals have holes.
The plan continues to enumerate every source unit, including standalone
metadata; CoreJS construction resolves each planned ID against the original
executable rows and therefore omits metadata without shifting later IDs. -/

namespace CoreJS.SparseProjectedOrder

/-- Original executable IDs in the order the full plan evaluates them. A plan
entry absent from `source` is metadata and is omitted only after module order
has been computed. -/
def executableIds (source : List (Nat × Stmt)) (P : EsmSplit.Plan) : List Nat :=
  P.execOrder.filter fun id => (indexedNumbered source id).isSome

/-- The executable part of the plan retains the source's original row order. -/
def OrderKept (source : List (Nat × Stmt)) (P : EsmSplit.Plan) : Prop :=
  (source.map Prod.fst).Nodup ∧ executableIds source P = source.map Prod.fst

instance (source : List (Nat × Stmt)) (P : EsmSplit.Plan) : Decidable (OrderKept source P) := by
  unfold OrderKept executableIds
  infer_instance

private theorem indexedNumbered_key (source : List (Nat × Stmt)) (id : Nat) {row : Nat × Stmt}
    (found : indexedNumbered source id = some row) : row.1 = id := by
  unfold indexedNumbered at found
  rcases Option.map_eq_some_iff.mp found with ⟨selected, _, rowEq⟩
  have keyEq := congrArg Prod.fst rowEq
  simpa using keyEq.symm

private theorem indexedNumbered_ids (source : List (Nat × Stmt)) : ∀ ids : List Nat,
    (ids.filterMap (indexedNumbered source)).map Prod.fst =
      ids.filter fun id => (indexedNumbered source id).isSome
  | [] => rfl
  | id :: ids => by
      simp only [List.filterMap_cons]
      cases h : indexedNumbered source id with
      | none => simp [h, indexedNumbered_ids source ids]
      | some row =>
          cases row with
          | mk key stmt =>
              have keyEq : key = id := indexedNumbered_key source id h
              subst key
              simp [h, indexedNumbered_ids source ids]

private theorem filterMap_eq_filter_isSome {α β : Type} (f : α → Option β) :
    ∀ ids : List α, ids.filterMap f = (ids.filter fun id => (f id).isSome).filterMap f
  | [] => rfl
  | id :: ids => by
      cases h : f id <;> simp [h, filterMap_eq_filter_isSome f ids]

private theorem filterMap_fst_of_lookup {source : List (Nat × Stmt)}
    (f : Nat → Option (Nat × Stmt))
    (lookup : ∀ row ∈ source, f row.1 = some row) :
    (source.map Prod.fst).filterMap f = source := by
  induction source with
  | nil => rfl
  | cons row rest ih =>
      have hrow := lookup row (by simp)
      have hrest : ∀ row ∈ rest, f row.1 = some row := fun row h => lookup row (by simp [h])
      simp only [List.map_cons, List.filterMap_cons, hrow]
      exact congrArg (row :: ·) (ih hrest)

private theorem indexedNumbered_of_mem {source : List (Nat × Stmt)} {row : Nat × Stmt}
    (keysNodup : (source.map Prod.fst).Nodup) (member : row ∈ source) :
    indexedNumbered source row.1 = some row := by
  obtain ⟨id, stmt⟩ := row
  have unique : ∀ other, (id, other) ∈ source → other = stmt := by
    intro other otherMember
    change (id, stmt) ∈ source at member
    exact (nodup_keys (k := id) (v := stmt) (v' := other) keysNodup member otherMember).symm
  simpa [indexedNumbered] using lookup_of_mem member unique

private theorem indexedNumbered_source_rows (source : List (Nat × Stmt))
    (keysNodup : (source.map Prod.fst).Nodup) :
    (source.map Prod.fst).filterMap (indexedNumbered source) = source :=
  filterMap_fst_of_lookup _ fun _ member => indexedNumbered_of_mem keysNodup member

private theorem start_le_foldl_max (values : List Nat) (start : Nat) :
    start ≤ values.foldl max start := by
  induction values generalizing start with
  | nil => exact Nat.le_refl _
  | cons value rest ih =>
      rw [List.foldl_cons]
      exact Nat.le_trans (Nat.le_max_left _ _) (ih (max start value))

private theorem mem_le_foldl_max (values : List Nat) (start value : Nat)
    (member : value ∈ values) : value ≤ values.foldl max start := by
  induction values generalizing start with
  | nil => simp at member
  | cons head rest ih =>
      rw [List.foldl_cons]
      rcases List.mem_cons.mp member with same | later
      · subst head
        exact Nat.le_trans (Nat.le_max_right _ _) (start_le_foldl_max rest (max start value))
      · exact ih (max start head) later

private theorem foldl_max_start_or_mem (values : List Nat) (start : Nat) :
    values.foldl max start = start ∨ values.foldl max start ∈ values := by
  induction values generalizing start with
  | nil => exact Or.inl rfl
  | cons value rest ih =>
      rw [List.foldl_cons]
      rcases ih (max start value) with same | member
      · rw [same]
        by_cases h : max start value = start
        · exact Or.inl h
        · have hvalue : max start value = value := by omega
          rw [hvalue]
          exact Or.inr (by simp)
      · exact Or.inr (by simp [member])

private theorem execOrder_mem_module {P : EsmSplit.Plan} {id : Nat}
    (member : id ∈ P.execOrder) : ∃ m ∈ P.mods, id ∈ m.stmts := by
  unfold EsmSplit.Plan.execOrder at member
  rcases List.mem_flatMap.mp member with ⟨name, _, member⟩
  cases found : P.find name with
  | none => simp [found] at member
  | some m =>
      simp only [found] at member
      exact ⟨m, List.mem_of_find?_eq_some found, member⟩

theorem split_indexed_find (source : List (Nat × Stmt)) (B : EsmSplit.Bundle) (P : EsmSplit.Plan)
    (m : String) : (splitProgramIndexed source B P).find m = (P.find m).map (moduleOfIndexed source B P) := by
  simp only [Program.find, splitProgramIndexed, EsmSplit.Plan.find, List.find?_map]
  rfl

/-- Membership in a projected module body means that the full plan assigned
the original ID to this module and the original source lookup supplies the
same statement. This is the construction premise used by sparse cell proofs. -/
theorem moduleOfIndexed_body_lookup (source : List (Nat × Stmt)) (B : EsmSplit.Bundle)
    (P : EsmSplit.Plan) (m : EsmSplit.Module) (id : Nat) (stmt : Stmt) :
    (id, stmt) ∈ (moduleOfIndexed source B P m).body ↔
      id ∈ m.stmts ∧ source.lookup id = some stmt := by
  change (id, stmt) ∈ m.stmts.filterMap (indexedNumbered source) ↔
    id ∈ m.stmts ∧ source.lookup id = some stmt
  constructor
  · intro member
    rcases List.mem_filterMap.mp member with ⟨planId, inPlan, row⟩
    have planIdEq : planId = id := (indexedNumbered_key source planId row).symm
    subst planId
    exact ⟨inPlan, by simpa [indexedNumbered] using row⟩
  · rintro ⟨inPlan, found⟩
    refine List.mem_filterMap.mpr ⟨id, inPlan, ?_⟩
    simpa [indexedNumbered] using found

private theorem split_indexed_visit (source : List (Nat × Stmt)) (B : EsmSplit.Bundle) (P : EsmSplit.Plan) :
    ∀ (fuel : Nat) (m : String) (st : List String × List String),
      (splitProgramIndexed source B P).visit fuel m st = P.visit fuel m st := by
  intro fuel m st
  unfold Program.visit EsmSplit.Plan.visit
  congr 1
  funext name
  rw [split_indexed_find]
  cases P.find name <;> simp [moduleOfIndexed, Function.comp_def]

private theorem split_indexed_evalOrder (source : List (Nat × Stmt)) (B : EsmSplit.Bundle)
    (P : EsmSplit.Plan) : (splitProgramIndexed source B P).evalOrder = P.evalOrder := by
  unfold Program.evalOrder EsmSplit.Plan.evalOrder
  have moduleCount : (splitProgramIndexed source B P).modules.length = P.mods.length := by
    simp [splitProgramIndexed]
  rw [moduleCount]
  exact congrArg Prod.snd (split_indexed_visit source B P _ _ _)

private theorem split_indexed_loaded_bodies (source : List (Nat × Stmt)) (B : EsmSplit.Bundle)
    (P : EsmSplit.Plan) : ∀ order : List String,
      ((order.filterMap (splitProgramIndexed source B P).find).flatMap (·.body)) =
        (order.flatMap fun m => ((P.find m).map (·.stmts)).getD []).filterMap (indexedNumbered source)
  | [] => rfl
  | m :: order => by
      rw [List.filterMap_cons, List.flatMap_cons, List.filterMap_append,
        ← split_indexed_loaded_bodies source B P order, split_indexed_find]
      cases P.find m <;> rfl

/-- The actual indexed CoreJS constructor enumerates precisely the executable
rows reached by the complete plan. Its statement values come from lookup by
original ID; plan-only metadata has no row and is omitted. -/
theorem split_indexed_loaded_flat (source : List (Nat × Stmt)) (B : EsmSplit.Bundle)
    (P : EsmSplit.Plan) :
    (splitProgramIndexed source B P).loaded.flatMap (·.body) =
      P.execOrder.filterMap (indexedNumbered source) := by
  rw [Program.loaded, split_indexed_evalOrder, split_indexed_loaded_bodies]
  rfl

/-- The loaded original identities are the projection of plan evaluation onto
the IDs for which the original source has an executable row. -/
theorem split_indexed_loaded_ids (source : List (Nat × Stmt)) (B : EsmSplit.Bundle)
    (P : EsmSplit.Plan) :
    ((splitProgramIndexed source B P).loaded.flatMap (·.body)).map Prod.fst = executableIds source P := by
  rw [split_indexed_loaded_flat]
  exact indexedNumbered_ids source P.execOrder

/-- Loaded rows come from the original bodies at the plan's executable IDs. -/
theorem split_indexed_loaded_at_ids (source : List (Nat × Stmt)) (B : EsmSplit.Bundle)
    (P : EsmSplit.Plan) :
    (splitProgramIndexed source B P).loaded.flatMap (·.body) =
      (executableIds source P).filterMap (indexedNumbered source) := by
  rw [split_indexed_loaded_flat, filterMap_eq_filter_isSome]
  rfl

theorem split_indexed_loaded_perm {source : List (Nat × Stmt)} {B : EsmSplit.Bundle} {P : EsmSplit.Plan}
    (unique : (source.map Prod.fst).Nodup)
    (covered : (executableIds source P).Perm (source.map Prod.fst)) :
    ((splitProgramIndexed source B P).loaded.flatMap (·.body)).Perm source := by
  rw [split_indexed_loaded_at_ids]
  have projected := covered.filterMap (indexedNumbered source)
  rw [indexedNumbered_source_rows source unique] at projected
  exact projected

/-- A checked row schedule agrees with the constructed body once the actual
plan has its IDs in the same order. -/
theorem split_indexed_loaded_of_ids {source scheduled : List (Nat × Stmt)}
    {B : EsmSplit.Bundle} {P : EsmSplit.Plan} (unique : (source.map Prod.fst).Nodup)
    (permuted : scheduled.Perm source) (ids : executableIds source P = scheduled.map Prod.fst) :
    (splitProgramIndexed source B P).loaded.flatMap (·.body) = scheduled := by
  rw [split_indexed_loaded_at_ids, ids]
  exact filterMap_fst_of_lookup _ fun _ member => indexedNumbered_of_mem unique (permuted.mem_iff.1 member)

/-- Under the projected order condition, the loaded rows equal the original sparse rows. -/
theorem split_indexed_loaded_eq_source {source : List (Nat × Stmt)} {B : EsmSplit.Bundle} {P : EsmSplit.Plan}
    (kept : OrderKept source P) :
    (splitProgramIndexed source B P).loaded.flatMap (·.body) = source := by
  rw [split_indexed_loaded_at_ids, kept.2]
  exact indexedNumbered_source_rows source kept.1

/-- Sorting a covered executable permutation recovers the original declaration allocation order. -/
theorem split_indexed_tops_of_perm {source : List (Nat × Stmt)} {B : EsmSplit.Bundle} {P : EsmSplit.Plan}
    (unique : (source.map Prod.fst).Nodup)
    (covered : (executableIds source P).Perm (source.map Prod.fst))
    (ordered : source.Pairwise fun a b => a.1 ≤ b.1) :
    (splitProgramIndexed source B P).tops = source := by
  unfold Program.tops
  have perm := split_indexed_loaded_perm (B := B) unique covered
  have sortedPerm : (((splitProgramIndexed source B P).loaded.flatMap (·.body)).mergeSort
      fun a b => decide (a.1 ≤ b.1)).Perm source :=
    (List.mergeSort_perm _ _).trans perm
  refine sortedPerm.eq_of_pairwise (le := fun a b => a.1 ≤ b.1) (fun a b ha hb hab hba => ?_) ?_ ordered
  · obtain ⟨i, x⟩ := a
    obtain ⟨j, y⟩ := b
    have same : i = j := Nat.le_antisymm hab hba
    subst j
    congr 1
    exact nodup_keys unique (sortedPerm.mem_iff.1 ha) hb
  · exact (List.pairwise_mergeSort (fun _ _ _ h h' => by simp at h h' ⊢; omega)
      (fun _ _ => by simp; omega) _).imp fun h => by simpa using h

/-- An order-kept plan is a special case of covered allocation. -/
theorem split_indexed_tops_eq_source {source : List (Nat × Stmt)} {B : EsmSplit.Bundle} {P : EsmSplit.Plan}
    (kept : OrderKept source P) (ordered : source.Pairwise fun a b => a.1 ≤ b.1) :
    (splitProgramIndexed source B P).tops = source :=
  split_indexed_tops_of_perm kept.1 (kept.2 ▸ List.Perm.refl _) ordered

/-- The actual constructor's region extent is the extent required by the
sparse source rows. Every source row is assigned by the kept projection, and
every constructed body row is selected by lookup from those same source rows.
This also accounts for plan modules that are not reached from the entry. -/
theorem split_indexed_regions_of_perm {source : List (Nat × Stmt)} {B : EsmSplit.Bundle} {P : EsmSplit.Plan}
    (unique : (source.map Prod.fst).Nodup)
    (covered : (executableIds source P).Perm (source.map Prod.fst)) :
    (splitProgramIndexed source B P).regions = (source.map fun row => row.1 + 1).foldl max 0 := by
  let extent := (source.map fun row => row.1 + 1).foldl max 0
  unfold Program.regions
  refine foldl_max_eq _ 0 extent (Nat.zero_le _) ?_ ?_
  · intro value member
    simp only [splitProgramIndexed, List.flatMap_map, List.mem_flatMap,
      List.mem_map] at member
    obtain ⟨m, _, row, inBody, valueEq⟩ := member
    obtain ⟨rowId, stmt⟩ := row
    have rowParts := (moduleOfIndexed_body_lookup source B P m rowId stmt).mp inBody
    have inSource : (rowId, stmt) ∈ source := lookup_mem rowParts.2
    subst value
    have inExtent : rowId + 1 ∈ source.map fun row => row.1 + 1 :=
      List.mem_map.mpr ⟨(rowId, stmt), inSource, rfl⟩
    exact mem_le_foldl_max (source.map fun row => row.1 + 1) 0 (rowId + 1) inExtent
  · rcases foldl_max_start_or_mem (source.map fun row => row.1 + 1) 0 with hzero | hmax
    · exact Or.inl hzero.symm
    · right
      rcases List.mem_map.mp hmax with ⟨⟨id, stmt⟩, inSource, rowEq⟩
      have inIds : id ∈ source.map Prod.fst := List.mem_map.mpr ⟨(id, stmt), inSource, rfl⟩
      have inFiltered : id ∈ executableIds source P := covered.mem_iff.2 inIds
      have inExec : id ∈ P.execOrder := (List.mem_filter.mp inFiltered).1
      obtain ⟨m, inMods, inBody⟩ := execOrder_mem_module inExec
      have sourceLookup : source.lookup id = some stmt := by
        have same : ∀ other, (id, other) ∈ source → other = stmt := by
          intro other otherMember
          exact (nodup_keys (k := id) (v := stmt) (v' := other) unique inSource otherMember).symm
        exact lookup_of_mem inSource same
      simp only [splitProgramIndexed, List.flatMap_map]
      apply List.mem_flatMap.mpr
      refine ⟨m, inMods, ?_⟩
      apply List.mem_map.mpr
      refine ⟨(id, stmt), (moduleOfIndexed_body_lookup source B P m id stmt).mpr ⟨inBody, sourceLookup⟩, ?_⟩
      simpa [extent] using rowEq

theorem split_indexed_regions_eq_source {source : List (Nat × Stmt)} {B : EsmSplit.Bundle} {P : EsmSplit.Plan}
    (kept : OrderKept source P) :
    (splitProgramIndexed source B P).regions = (source.map fun row => row.1 + 1).foldl max 0 :=
  split_indexed_regions_of_perm kept.1 (kept.2 ▸ List.Perm.refl _)

/-- The split constructor's file placements are the authored plan rows after
projection through the sparse executable source. Metadata remains in the plan
but does not acquire a CoreJS unit placement. -/
private theorem indexedPlacementsOfModules (source : List (Nat × Stmt)) (B : EsmSplit.Bundle)
    (P : EsmSplit.Plan) : ∀ mods : List EsmSplit.Module,
      (∀ m ∈ mods, EsmSplit.safeModulePath m.name = true) →
      List.flatMap Module.unitPlacements
          ((mods.map (moduleOfIndexed source B P)).filter fun m => EsmSplit.safeModulePath m.name) =
        List.flatMap (fun m => (m.stmts.filterMap (indexedNumbered source)).map fun row => (row.1, m.name)) mods
  | [] => fun _ => rfl
  | m :: rest => fun safe => by
      have safeHead := safe m (by simp)
      have safeTail : ∀ m ∈ rest, EsmSplit.safeModulePath m.name = true :=
        fun m hm => safe m (by simp [hm])
      simp only [List.map_cons, List.filter_cons, safeHead, ↓reduceIte,
        List.flatMap_cons, moduleOfIndexed, Module.unitPlacements]
      rw [indexedPlacementsOfModules source B P rest safeTail]

theorem split_indexed_fileUnitPlacements {source : List (Nat × Stmt)} {B : EsmSplit.Bundle} {P : EsmSplit.Plan}
    (safe : ∀ m ∈ P.mods, EsmSplit.safeModulePath m.name = true) :
    (splitProgramIndexed source B P).fileUnitPlacements =
      P.mods.flatMap fun m => (m.stmts.filterMap (indexedNumbered source)).map fun row => (row.1, m.name) := by
  unfold Program.fileUnitPlacements
  rw [splitProgramIndexed]
  exact indexedPlacementsOfModules source B P P.mods safe

/-- File units emitted by the indexed split have their unique authored
owners. The premise is the same faithful-plan check used by the planner; this
theorem connects it to placements on the real constructed program. -/
theorem split_indexed_authored_owner {source : List (Nat × Stmt)} {B : EsmSplit.Bundle}
    {S : EsmSplit.Spec} {P : EsmSplit.Plan}
    (faithful : EsmSplit.Faithful B S P)
    (safe : ∀ m ∈ P.mods, EsmSplit.safeModulePath m.name = true)
    {id : Nat} {owner : String}
    (member : (id, owner) ∈ (splitProgramIndexed source B P).fileUnitPlacements) :
    S.claimants B id = [owner] := by
  rw [split_indexed_fileUnitPlacements safe] at member
  rcases List.mem_flatMap.mp member with ⟨m, inMods, placement⟩
  rcases List.mem_map.mp placement with ⟨row, rowMember, ownerEq⟩
  obtain ⟨rowId, stmt⟩ := row
  have planMember : rowId ∈ m.stmts := by
    rcases List.mem_filterMap.mp rowMember with ⟨planId, inStmts, lookup⟩
    have planIdEq : planId = rowId := (indexedNumbered_key source planId lookup).symm
    simpa [planIdEq] using inStmts
  have rowIdEq : rowId = id := congrArg Prod.fst ownerEq
  have ownerIdEq : m.name = owner := congrArg Prod.snd ownerEq
  subst id
  subst owner
  apply CoreJS.PlanPlacement.authored_owner faithful
  unfold CoreJS.PlanPlacement.assignments
  exact List.mem_flatMap.mpr ⟨m, inMods, List.mem_map.mpr ⟨rowId, planMember, rfl⟩⟩

/-- Equal tops and region extents make `declareTops` start from exactly the
same state and allocate the same frames for a sparse source and its split. -/
theorem split_indexed_declareTops_initial {source : List (Nat × Stmt)}
    {B : EsmSplit.Bundle} {P : EsmSplit.Plan} (kept : OrderKept source P)
    (ordered : source.Pairwise fun a b => a.1 ≤ b.1) (w : World) :
    declareTops (splitProgramIndexed source B P).tops
        (initialState w (splitProgramIndexed source B P).regions) =
      declareTops source (initialState w ((source.map fun row => row.1 + 1).foldl max 0)) := by
  rw [split_indexed_regions_eq_source kept, split_indexed_tops_eq_source kept ordered]

/-- For any source rows and plan, keeping the projected original IDs proves
that linking's loaded units carry the same ID schedule. This theorem is about
the real plan-derived CoreJS program, not only a list-level projection. -/
theorem loaded_ids_of_order_kept {source : List (Nat × Stmt)} {B : EsmSplit.Bundle} {P : EsmSplit.Plan}
    (kept : OrderKept source P) :
    ((splitProgramIndexed source B P).loaded.flatMap (·.body)).map Prod.fst = source.map Prod.fst := by
  rw [split_indexed_loaded_ids]
  exact kept.2

/-- A statement in the constructed program is the source row selected by its
original identity. This is the value-level part of the projection contract. -/
theorem loaded_row_lookup {source : List (Nat × Stmt)} {B : EsmSplit.Bundle} {P : EsmSplit.Plan}
    {id : Nat} {stmt : Stmt}
    (member : (id, stmt) ∈ (splitProgramIndexed source B P).loaded.flatMap (·.body)) :
    indexedNumbered source id = some (id, stmt) := by
  rw [split_indexed_loaded_flat] at member
  rcases List.mem_filterMap.mp member with ⟨planId, _, lookup⟩
  unfold indexedNumbered at lookup
  simp only [Option.map_eq_some_iff, Prod.mk.injEq] at lookup
  rcases lookup with ⟨row, found, idEq, stmtEq⟩
  subst idEq
  subst stmtEq
  simp [indexedNumbered, found]

/-- A faithful `mkPlan` retains the spec's unique owner for every planned
unit, including a non-executable metadata ordinal. -/
theorem authored_owner {B : EsmSplit.Bundle} {S : EsmSplit.Spec} {P : EsmSplit.Plan}
    (faithful : EsmSplit.Faithful B S P) {id : Nat} {owner : String}
    (member : (id, owner) ∈ CoreJS.PlanPlacement.assignments P) : S.claimants B id = [owner] :=
  CoreJS.PlanPlacement.authored_owner faithful member

#print axioms split_indexed_loaded_perm
#print axioms split_indexed_tops_of_perm
#print axioms split_indexed_regions_of_perm
#print axioms split_indexed_loaded_flat
#print axioms split_indexed_find
#print axioms moduleOfIndexed_body_lookup
#print axioms split_indexed_loaded_ids
#print axioms loaded_ids_of_order_kept
#print axioms loaded_row_lookup
#print axioms authored_owner

end CoreJS.SparseProjectedOrder
