import EsmSplit.CoreJS.DeploymentPlacement
import EsmSplit.CoreJS.Split
import EsmSplit.Split

/-! Original-unit placement derived from source and actual plan, without
inspecting or running a candidate program. -/

namespace CoreJS.PlanPlacement

/-- The executable unit IDs and owners requested by the plan. -/
def assignments (P : EsmSplit.Plan) : List UnitPlacement :=
  P.mods.flatMap fun m => m.stmts.map fun id => (id, m.name)

private theorem numberedPlacements (ss : List Stmt) (owner : String) (ids : List Nat)
    (bounded : ∀ id ∈ ids, id < ss.length) :
    (ids.filterMap (fun i => (ss[i]?).map (i, ·))).map (fun p => (p.1, owner)) =
      ids.map (fun id => (id, owner)) := by
  induction ids with
  | nil => rfl
  | cons id rest ih =>
    have h : id < ss.length := bounded id (by simp)
    have hrest : ∀ id ∈ rest, id < ss.length := fun id hi => bounded id (by simp [hi])
    simp [h, ih hrest]

private theorem collectModules (ss : List Stmt) (B : EsmSplit.Bundle) (P : EsmSplit.Plan)
    (mods : List EsmSplit.Module) (safe : ∀ m ∈ mods, EsmSplit.safeModulePath m.name = true)
    (bounded : ∀ m ∈ mods, ∀ id ∈ m.stmts, id < ss.length) :
    ((mods.map (moduleOf ss B P)).filter (fun m => EsmSplit.safeModulePath m.name)).flatMap
        Module.unitPlacements =
      mods.flatMap (fun m => m.stmts.map fun id => (id, m.name)) := by
  induction mods with
  | nil => rfl
  | cons m rest ih =>
    have hm : EsmSplit.safeModulePath m.name = true := safe m (by simp)
    have hbound : ∀ id ∈ m.stmts, id < ss.length := bounded m (by simp)
    have hs : ∀ m ∈ rest, EsmSplit.safeModulePath m.name = true := fun m h => safe m (by simp [h])
    have hb : ∀ m ∈ rest, ∀ id ∈ m.stmts, id < ss.length := fun m h => bounded m (by simp [h])
    simp only [List.map_cons, List.filter_cons, moduleOf, hm, ↓reduceIte, List.flatMap_cons,
      Module.unitPlacements]
    have hplaced :
      (m.stmts.filterMap (fun i => (ss[i]?).map (i, ·))).map (fun p => (p.1, m.name)) =
        m.stmts.map (fun id => (id, m.name)) := by
      exact numberedPlacements ss m.name m.stmts hbound
    rw [hplaced, ih hs hb]

/-- The actual planner output contains precisely the plan's labeled units,
provided every planned ID is in the source and every owner is a file path. -/
theorem output_assignments (ss : List Stmt) (B : EsmSplit.Bundle) (P : EsmSplit.Plan)
    (safe : ∀ m ∈ P.mods, EsmSplit.safeModulePath m.name = true)
    (bounded : ∀ m ∈ P.mods, ∀ id ∈ m.stmts, id < ss.length) :
    (splitProgram ss B P).fileUnitPlacements = assignments P := by
  exact collectModules ss B P P.mods safe bounded

/-- A faithful plan labels each unit with its unique authored claimant. -/
theorem authored_owner {B : EsmSplit.Bundle} {S : EsmSplit.Spec} {P : EsmSplit.Plan}
    (faithful : EsmSplit.Faithful B S P) {id : Nat} {owner : String}
    (member : (id, owner) ∈ assignments P) : S.claimants B id = [owner] := by
  unfold assignments at member
  obtain ⟨m, hm, hrow⟩ := List.mem_flatMap.mp member
  obtain ⟨i, hi, eq⟩ := List.mem_map.mp hrow
  cases eq
  have inRange : id ∈ List.range B.length := List.mem_range.mpr (faithful.2.2.2.2.2.1 m hm id hi)
  exact (faithful.2.2.2.2.1 m hm id inRange).1 hi

/-- If a source/plan check establishes exact coverage, the actual split
satisfies the ordinary `Places` predicate for the plan assignment. -/
theorem planned_places (ss : List Stmt) (B : EsmSplit.Bundle) (P : EsmSplit.Plan) (name : String)
    (exports : List (Name × Name)) (sourceSafe : EsmSplit.safeModulePath name = true)
    (safe : ∀ m ∈ P.mods, EsmSplit.safeModulePath m.name = true)
    (coverage : ((assignments P).map Prod.fst).Perm (List.range ss.length)) :
    Places (assignments P) ⟨name, [⟨name, [], (List.range ss.length).zip ss, exports⟩]⟩
      (splitProgram ss B P) := by
  have bounded : ∀ m ∈ P.mods, ∀ id ∈ m.stmts, id < ss.length := by
    intro m hm id hi
    have inAssignments : (id, m.name) ∈ assignments P := by
      exact List.mem_flatMap.mpr ⟨m, hm, List.mem_map.mpr ⟨id, hi, rfl⟩⟩
    have inIds : id ∈ (assignments P).map Prod.fst :=
      List.mem_map.mpr ⟨(id, m.name), inAssignments, rfl⟩
    exact List.mem_range.mp (coverage.mem_iff.mp inIds)
  refine ⟨coverage.nodup_iff.mpr List.nodup_range, ?_, ?_⟩
  · unfold Program.fileUnitPlacements Module.unitPlacements
    simp only [sourceSafe, List.filter_cons, ↓reduceIte, List.filter_nil, List.flatMap_cons,
      List.flatMap_nil, List.append_nil]
    rw [List.map_map]
    change (((List.range ss.length).zip ss).map Prod.fst).Perm ((assignments P).map Prod.fst)
    rw [List.map_fst_zip (by simp)]
    exact coverage.symm
  · rw [output_assignments ss B P safe bounded]

/-- On the actual authored plan, every source unit is placed once and its
owner is the unique module claimed by the specification. The coverage and
path checks inspect only the source length and the plan. -/
theorem faithful_planned_places (ss : List Stmt) (B : EsmSplit.Bundle) (S : EsmSplit.Spec)
    (name : String) (exports : List (Name × Name))
    (sourceSafe : EsmSplit.safeModulePath name = true)
    (safe : ∀ m ∈ (EsmSplit.mkPlan B S).mods, EsmSplit.safeModulePath m.name = true)
    (faithful : EsmSplit.Faithful B S (EsmSplit.mkPlan B S))
    (coverage : ((assignments (EsmSplit.mkPlan B S)).map Prod.fst).Perm (List.range ss.length)) :
    Places (assignments (EsmSplit.mkPlan B S))
        ⟨name, [⟨name, [], (List.range ss.length).zip ss, exports⟩]⟩
        (splitProgram ss B (EsmSplit.mkPlan B S)) ∧
      ∀ id owner, (id, owner) ∈ assignments (EsmSplit.mkPlan B S) → S.claimants B id = [owner] := by
  exact
    ⟨planned_places ss B (EsmSplit.mkPlan B S) name exports sourceSafe safe coverage,
      fun _ _ member => authored_owner faithful member⟩

end CoreJS.PlanPlacement
