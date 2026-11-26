import EsmSplit.CoreJS.Split

/-! A plan can retain terminal non-executable source units while the actual
CoreJS construction omits them from module bodies. The projection here keeps
the original executable IDs; it never creates a statement for metadata. The
caller must separately prove that filtered IDs are valid metadata with the
authored owners and exports; these theorems do not establish that premise. -/

namespace CoreJS.ProjectedOrder

/-- The plan's executable IDs, in the authored evaluation order. This dense
form admits metadata positions after the last executable source unit. -/
def executable (ss : List Stmt) (P : EsmSplit.Plan) : List Nat :=
  P.execOrder.filter fun id => id < ss.length

/-- The executable units run in their original source order. -/
def Kept (ss : List Stmt) (P : EsmSplit.Plan) : Prop := executable ss P = List.range ss.length

instance (ss : List Stmt) (P : EsmSplit.Plan) : Decidable (Kept ss P) := by
  unfold Kept
  infer_instance

/-- A valid all-unit plan containing a terminal metadata position cannot
satisfy the older order predicate that requires every planned ID to be an
executable ID. -/
theorem full_order_incompatible {ss : List Stmt} {B : EsmSplit.Bundle} {S : EsmSplit.Spec} {P : EsmSplit.Plan}
    (valid : EsmSplit.Valid B S P) (terminal : ss.length < B.length) : ¬ OrderKept ss P := by
  intro kept
  obtain ⟨-, -, -, -, -, -, -, ⟨-, -, all⟩, -⟩ := valid
  have present : ss.length ∈ P.execOrder := all ss.length (List.mem_range.2 terminal)
  rw [kept] at present
  exact (Nat.lt_irrefl ss.length) (List.mem_range.1 present)

/-- Removing non-executable IDs does not change the statements `moduleOf`
constructs; the remaining IDs are not renumbered. -/
theorem numbered_filter (ss : List Stmt) (ids : List Nat) :
    (ids.filter fun id => id < ss.length).filterMap (numbered ss) = ids.filterMap (numbered ss) := by
  induction ids with
  | nil => rfl
  | cons id rest ih =>
    by_cases h : id < ss.length
    · simp only [List.filter_cons, h, decide_true, ↓reduceIte, List.filterMap_cons]
      rw [ih]
    · have noStmt : ss[id]? = none := List.getElem?_eq_none (Nat.le_of_not_gt h)
      simp only [List.filter_cons, h, decide_false, Bool.false_eq_true, ↓reduceIte, List.filterMap_cons]
      simpa [numbered, noStmt] using ih

/-- After projecting metadata, the actual split has the original top-level
statements and their original identities. -/
theorem split_tops (ss : List Stmt) (B : EsmSplit.Bundle) (P : EsmSplit.Plan)
    (coverage : (executable ss P).Perm (List.range ss.length)) :
    (splitProgram ss B P).tops = (List.range ss.length).zip ss := by
  unfold Program.tops
  rw [split_loaded_flat, ← numbered_filter]
  exact mergeSort_numbered ss coverage

/-- Metadata does not add an allocation region to the actual constructed
program; every executable source region is still present. -/
theorem split_regions (ss : List Stmt) (B : EsmSplit.Bundle) (P : EsmSplit.Plan)
    (coverage : (executable ss P).Perm (List.range ss.length)) :
    (splitProgram ss B P).regions = ss.length := by
  unfold Program.regions
  refine foldl_max_eq _ 0 _ (Nat.zero_le _) ?_ ?_
  · simp only [splitProgram, List.flatMap_map, List.mem_flatMap, List.mem_map]
    rintro _ ⟨md, _, ⟨i, s⟩, hi, rfl⟩
    have := ((mem_numbered ss).1 hi).2
    have := (List.getElem?_eq_some_iff.1 this).1
    simp only; omega
  · rcases Nat.eq_zero_or_pos ss.length with h | h
    · exact Or.inl h.symm
    · right
      have projected : ss.length - 1 ∈ executable ss P :=
        coverage.mem_iff.2 (List.mem_range.2 (by omega))
      have hmem : ss.length - 1 ∈ P.execOrder := (List.mem_filter.1 projected).1
      simp only [EsmSplit.Plan.execOrder, List.mem_flatMap] at hmem
      obtain ⟨m, _, hm⟩ := hmem
      cases hf : P.find m with
      | none => simp [hf] at hm
      | some md =>
        simp only [hf, Option.map_some, Option.getD_some] at hm
        simp only [splitProgram, List.flatMap_map, List.mem_flatMap, List.mem_map]
        refine ⟨md, List.mem_of_find?_eq_some hf, (ss.length - 1, ss[ss.length - 1]), ?_, Nat.sub_add_cancel h⟩
        exact (mem_numbered ss).2 ⟨hm, by simp⟩

/-- The projected coverage premise supplies the unchanged regions and tops
needed by the existing all-world schedule theorem. -/
theorem holds_statements {ss : List Stmt} {B : EsmSplit.Bundle} {P : EsmSplit.Plan}
    (coverage : (executable ss P).Perm (List.range ss.length)) :
    (Program.single ss).regions = (splitProgram ss B P).regions ∧
    (Program.single ss).tops = (splitProgram ss B P).tops := by
  rw [single_regions, split_regions ss B P coverage, single_tops, split_tops ss B P coverage]
  exact ⟨rfl, rfl⟩

/-- Once a metadata-aware source/plan proof supplies scope agreement, the
projected order composes with the existing all-world execution theorem. This
statement does not itself establish that scope agreement. -/
theorem run_same {ss : List Stmt} {B : EsmSplit.Bundle} {P : EsmSplit.Plan}
    (coverage : (executable ss P).Perm (List.range ss.length))
    (scopes : ScopesAgree (Program.single ss) (splitProgram ss B P)) (w : World) (fuel : Nat) :
    runProgram w fuel (Program.single ss) = runProgram w fuel (splitProgram ss B P) :=
  CoreJS.run_same (holds_statements coverage).1 (holds_statements coverage).2 scopes fuel

#print axioms numbered_filter
#print axioms full_order_incompatible
#print axioms split_tops
#print axioms split_regions
#print axioms holds_statements
#print axioms run_same

end CoreJS.ProjectedOrder
