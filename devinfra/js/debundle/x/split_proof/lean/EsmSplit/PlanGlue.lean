import EsmSplit.Split

/-! Facts about public aliases and the glue exports used by the actual planner.
The source publication and imported local names are checked independently of
any resulting export table. -/

namespace EsmSplit.PlanGlue

/-- Every consumed local already appears in the source's local export metadata,
possibly under a different public alias. -/
def Covered (published : List (Name × Option Name)) (needed : List Name) : Prop :=
  ∀ name ∈ needed, name ∈ (localExports published).map Prod.snd

instance (published : List (Name × Option Name)) (needed : List Name) :
    Decidable (Covered published needed) := by
  unfold Covered
  infer_instance

/-- Membership in the source-local set is exactly alias-aware local
publication, with no dependence on the emitted export table. -/
theorem published_contains_iff (published : List (Name × Option Name)) (name : Name) :
    ((localExports published).foldl (fun acc e => acc.insert e.2) (∅ : NameSet)).contains name = true ↔
      name ∈ (localExports published).map Prod.snd := by
  have folded : (localExports published).foldl (fun acc e => acc.insert e.2) (∅ : NameSet) =
      ((localExports published).map Prod.snd).foldl (·.insert ·) (∅ : NameSet) := by
    simp [List.foldl_map]
  rw [folded, ← Std.TreeSet.mem_iff_contains, mem_foldl_insert]
  simp

private theorem glue_fold_length (names : List Name) (acc : List (Name × Name) × NameSet) :
    ((names.foldl (fun (state : List (Name × Name) × NameSet) name =>
      let publicName := freshName name state.2
      ((publicName, name) :: state.1, state.2.insert publicName)) acc).1).length =
      acc.1.length + names.length := by
  induction names generalizing acc with
  | nil => simp
  | cons name rest ih =>
    simp only [List.foldl_cons]
    rw [ih]
    simp only [List.length_cons]
    omega

/-- Every consumed local which is not already published creates one glue
export. No generated public name is hidden by this calculation. -/
theorem glue_length (published : List (Name × Option Name)) (needed : List Name) :
    (glueExports published needed).length =
      (needed.filter fun name => !((localExports published).foldl
        (fun acc e => acc.insert e.2) (∅ : NameSet)).contains name).length := by
  unfold glueExports
  simp only [List.length_reverse]
  simpa using glue_fold_length
    (needed.filter fun name => !((localExports published).foldl
      (fun acc e => acc.insert e.2) (∅ : NameSet)).contains name)
    ([], published.foldl (fun acc e => acc.insert e.1) (∅ : NameSet))

/-- The planner generates no glue when each consumed local is already exported;
this derives absence from source metadata and needs, not from a final table. -/
theorem glue_empty_of_covered {published : List (Name × Option Name)} {needed : List Name}
    (covered : Covered published needed) : glueExports published needed = [] := by
  have filtered : needed.filter (fun u =>
      !((localExports published).foldl (fun acc e => acc.insert e.2) (∅ : NameSet)).contains u) = [] := by
    apply List.eq_nil_iff_forall_not_mem.mpr
    intro name member
    simp only [List.mem_filter, Bool.not_eq_true'] at member
    have present := (published_contains_iff published name).2 (covered name member.1)
    simp [present] at member
  simp [glueExports, filtered]

/-- If glue is empty, every consumed local was already published. Thus the
coverage check rejects exactly when the planner must add a public name. -/
theorem covered_of_glue_empty {published : List (Name × Option Name)} {needed : List Name}
    (empty : glueExports published needed = []) : Covered published needed := by
  have lengthZero : (needed.filter fun name => !((localExports published).foldl
      (fun acc e => acc.insert e.2) (∅ : NameSet)).contains name).length = 0 := by
    rw [← glue_length]
    simp [empty]
  have filterEmpty : (needed.filter fun name => !((localExports published).foldl
      (fun acc e => acc.insert e.2) (∅ : NameSet)).contains name) = [] := by
    cases h : (needed.filter fun name => !((localExports published).foldl
        (fun acc e => acc.insert e.2) (∅ : NameSet)).contains name) with
    | nil => rfl
    | cons first rest => simp [h] at lengthZero
  intro name member
  have present : ((localExports published).foldl (fun acc e => acc.insert e.2) (∅ : NameSet)).contains name = true := by
    cases h : ((localExports published).foldl (fun acc e => acc.insert e.2) (∅ : NameSet)).contains name with
    | true => rfl
    | false =>
      have inFilter : name ∈ needed.filter (fun name => !((localExports published).foldl
          (fun acc e => acc.insert e.2) (∅ : NameSet)).contains name) := by
        simp [member, h]
      rw [filterEmpty] at inFilter
      cases inFilter
  exact (published_contains_iff published name).1 present

/-- Finishing a plan changes imports only when adding orphan edges. The glue
for every resulting module is computed from its own source metadata and the
map of named imports consumed by the actual draft. -/
theorem finishPlan_glue (B : Bundle) (S : Spec) (base : List Module) (rank : ModName → Nat)
    {md : Module} (member : md ∈ (finishPlan B S base rank).1.mods) :
    md.exports = glueExports (md.published B)
      (dedup (((finishPlan B S base rank).2.getD md.name []).reverse)) := by
  simp only [finishPlan, List.mem_map] at member
  obtain ⟨mid, ⟨original, originalIn, rfl⟩, rfl⟩ := member
  simp only [finishPlan]
  split <;> simp [Module.published, Module.publishedFrom]

/-- Coverage is checked against names accumulated from the actual plan's named
import edges; orphan edges added afterward contain no names. -/
def EntryCovered (B : Bundle) (S : Spec) (entry : Module) : Prop :=
  Covered (entry.published B)
    (dedup (((mkPlanWithImports B S).2.getD entry.name []).reverse))

instance (B : Bundle) (S : Spec) (entry : Module) : Decidable (EntryCovered B S entry) := by
  unfold EntryCovered
  infer_instance

/-- The entry glue is computed from source metadata and the actual planner's
consumed-name map. This equality does not compare output export tables. -/
theorem mkPlan_glue_equation {B : Bundle} {S : Spec} {entry : Module}
    (found : (mkPlan B S).find (mkPlan B S).entry = some entry) :
    entry.exports = glueExports (entry.published B)
      (dedup (((mkPlanWithImports B S).2.getD entry.name []).reverse)) := by
  have member : entry ∈ (mkPlan B S).mods := List.mem_of_find?_eq_some found
  unfold mkPlan mkPlanWithImports at member
  unfold mkPlanWithImports
  exact finishPlan_glue _ _ _ _ member

/-- Every found actual plan module has no extra glue if its source metadata
already publishes every local consumed from it, including through an alias. -/
theorem mkPlan_no_extra_glue {B : Bundle} {S : Spec} {entry : Module}
    (found : (mkPlan B S).find (mkPlan B S).entry = some entry)
    (covered : EntryCovered B S entry) : entry.exports = [] := by
  exact (mkPlan_glue_equation found).trans (glue_empty_of_covered covered)

/-- Coverage is exact for a found plan entry: refusing an uncovered consumed
local corresponds to a nonempty generated entry glue list. -/
theorem mkPlan_no_extra_glue_iff {B : Bundle} {S : Spec} {entry : Module}
    (found : (mkPlan B S).find (mkPlan B S).entry = some entry) :
    entry.exports = [] ↔ EntryCovered B S entry := by
  constructor
  · intro empty
    apply covered_of_glue_empty
    exact (mkPlan_glue_equation found).symm.trans empty
  · exact mkPlan_no_extra_glue found


#print axioms published_contains_iff
#print axioms glue_length
#print axioms covered_of_glue_empty
#print axioms glue_empty_of_covered
#print axioms finishPlan_glue
#print axioms mkPlan_glue_equation
#print axioms mkPlan_no_extra_glue
#print axioms mkPlan_no_extra_glue_iff

end EsmSplit.PlanGlue
