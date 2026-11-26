import EsmSplit.CoreJS.Split
import EsmSplit.CoreJS.LinkedBehavior
import EsmSplit.Split

/-! A plan can retain terminal non-executable source units while the actual
CoreJS construction omits them from module bodies. The projection here keeps
the original executable IDs; it never creates a statement for metadata. The
caller must separately prove that filtered IDs are valid metadata with the
authored owners and exports; these theorems do not establish that premise. -/

namespace CoreJS.ProjectedOrder

open LinkedBehavior

/-- This projection applies only when the original executable IDs are dense
and the single metadata unit is exactly the immediately following source ID.
The caller still supplies provenance that the last unit is metadata. -/
def TerminalOnly (source : Module) (metadataOrdinal : Nat) : Prop :=
  source.body.map Prod.fst = List.range source.body.length ∧ metadataOrdinal = source.body.length

instance (source : Module) (metadataOrdinal : Nat) : Decidable (TerminalOnly source metadataOrdinal) := by
  unfold TerminalOnly
  infer_instance

/-- Reject sparse executable IDs before applying the terminal-only projection. -/
def checkTerminalOnly (source : Module) (metadataOrdinal : Nat) : Except String (PLift (TerminalOnly source metadataOrdinal)) :=
  if h : TerminalOnly source metadataOrdinal then .ok ⟨h⟩
  else .error "expected dense executable original IDs followed by one terminal metadata unit"

/-- Under that source-side check, compact body positions are exactly the
original executable IDs. No later executable unit is renumbered. -/
theorem terminal_body (source : Module) {metadataOrdinal : Nat} (shape : TerminalOnly source metadataOrdinal) :
    source.body = (List.range (source.body.map Prod.snd).length).zip (source.body.map Prod.snd) := by
  symm
  rw [List.length_map, ← shape.1, List.zip_map']
  exact List.map_id _

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

/-- IDs remaining in numbered module bodies are precisely the filtered IDs. -/
theorem numbered_ids (ss : List Stmt) (ids : List Nat) :
    (ids.filterMap (numbered ss)).map Prod.fst = ids.filter (fun id => id < ss.length) := by
  induction ids with
  | nil => rfl
  | cons id rest ih =>
    by_cases h : id < ss.length
    · simp [numbered, h, ih]
    · simp [numbered, h, ih]

/-- Project after computing the full plan's module graph and evaluation order:
the actual loaded module bodies have precisely those original IDs. -/
theorem loaded_ids (ss : List Stmt) (B : EsmSplit.Bundle) (P : EsmSplit.Plan) :
    ((splitProgram ss B P).loaded.flatMap (·.body)).map Prod.fst = executable ss P := by
  rw [split_loaded_flat, numbered_ids]
  rfl

/-- Successful scope construction preserves the full loaded module list. -/
theorem scopes_modules (program : Program) (frames : List (Nat × List (Name × Addr))) :
    ∀ (modules : List Module) (linked : List (Module × Env)),
      program.scopes frames modules = .ok linked → linked.map Prod.fst = modules
  | [], linked, h => by
    change Except.ok [] = .ok linked at h
    cases h
    rfl
  | m :: rest, linked, h => by
    change ((do
      let imported ← program.imported frames m.imports
      let tail ← program.scopes frames rest
      pure ((m, { vars := m.scope frames ++ imported, this := .undef }) :: tail)) : Except Abort _)
      = .ok linked at h
    cases imports : program.imported frames m.imports with
    | error why => rw [imports] at h; cases h
    | ok cells =>
      cases later : program.scopes frames rest with
      | error why => rw [imports, later] at h; cases h
      | ok tail =>
        rw [imports, later] at h
        cases h
        simp only [List.map_cons]
        exact congrArg (m :: ·) (scopes_modules program frames rest tail later)

/-- Whenever the real link succeeds, its unit IDs are the executable
projection of the full plan order. This conditional fact does not assume
link success for certification or assert anything about link failures. -/
theorem linked_ids (ss : List Stmt) (B : EsmSplit.Bundle) (P : EsmSplit.Plan)
    {w : World} (before : CheckedLink (splitProgram ss B P) w) :
    (unitsOf before.linked).map Prod.fst = executable ss P := by
  let program := splitProgram ss B P
  have linked := before.equation
  change link program (initialState w program.regions) = .ok before.linked before.ready at linked
  unfold link linkUsingScopes at linked
  obtain ⟨_, afterCheck, _, linked⟩ := bind_ok linked
  obtain ⟨frames, afterDeclare, _, linked⟩ := bind_ok linked
  change ((lift (program.scopes frames program.loaded) >>= fun linked => do
      initFunctions ((unitsOf linked).mergeSort fun a b => decide (a.1 ≤ b.1))
      pure linked) : M w _) afterDeclare = .ok before.linked before.ready at linked
  cases result : program.scopes frames program.loaded with
  | error why => cases why <;> simp [result, lift, M.bind_apply, throwErr, throwVal, unknown] at linked
  | ok scope =>
    rw [result] at linked
    change (initFunctions ((unitsOf scope).mergeSort fun a b => decide (a.1 ≤ b.1)) >>= fun _ => pure scope)
      afterDeclare = .ok before.linked before.ready at linked
    obtain ⟨_, _, _, returned⟩ := bind_ok linked
    change Res.ok scope _ = .ok before.linked before.ready at returned
    have eq : scope = before.linked := by injection returned
    have modules := scopes_modules program frames program.loaded scope result
    have idkeys : (unitsOf scope).map Prod.fst = (scope.flatMap (·.1.body)).map Prod.fst := by
      simpa only [List.map_map, Function.comp_def] using congrArg (List.map Prod.fst) (unitsOf_keys scope)
    rw [← eq, idkeys]
    rw [show scope.flatMap (·.1.body) = (scope.map Prod.fst).flatMap (·.body) by rw [List.flatMap_map]]
    rw [modules]
    exact loaded_ids ss B P

/-- For an admitted terminal-only source and an order-kept full plan, the
actual loaded module bodies retain exactly the original executable ID order.
The full graph still determines module ordering before projection. -/
theorem terminal_loaded_ids (source : Module) (metadataOrdinal : Nat) (B : EsmSplit.Bundle) (P : EsmSplit.Plan)
    (shape : TerminalOnly source metadataOrdinal) (kept : Kept (source.body.map Prod.snd) P) :
    ((splitProgram (source.body.map Prod.snd) B P).loaded.flatMap (·.body)).map Prod.fst = source.body.map Prod.fst := by
  rw [loaded_ids, kept]
  simpa only [List.length_map] using shape.1.symm

/-- If the actual full-plan link succeeds, the linked execution schedule has
the same original IDs. No success premise is introduced into certification. -/
theorem terminal_linked_ids (source : Module) (metadataOrdinal : Nat) (B : EsmSplit.Bundle) (P : EsmSplit.Plan)
    (shape : TerminalOnly source metadataOrdinal) (kept : Kept (source.body.map Prod.snd) P)
    {w : World} (before : CheckedLink (splitProgram (source.body.map Prod.snd) B P) w) :
    (unitsOf before.linked).map Prod.fst = source.body.map Prod.fst := by
  rw [linked_ids, kept]
  simpa only [List.length_map] using shape.1.symm

/-- An executable original unit occurs in exactly the module claimed by a
faithful authored specification; this says nothing about metadata ownership. -/
theorem authored_executable_owner {ss : List Stmt} {B : EsmSplit.Bundle} {S : EsmSplit.Spec}
    {m : EsmSplit.Module} {id : Nat}
    (faithful : EsmSplit.Faithful B S (EsmSplit.mkPlan B S))
    (member : m ∈ (EsmSplit.mkPlan B S).mods) (inSource : id < ss.length) (inBundle : id < B.length) :
    (id, ss[id]) ∈ (moduleOf ss B (EsmSplit.mkPlan B S) m).body ↔ S.claimants B id = [m.name] := by
  change (id, ss[id]) ∈ m.stmts.filterMap (numbered ss) ↔ S.claimants B id = [m.name]
  rw [mem_numbered]
  have present : ss[id]? = some ss[id] := by simp
  simp only [present, and_true]
  exact faithful.2.2.2.2.1 m member id (List.mem_range.mpr inBundle)

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
#print axioms numbered_ids
#print axioms checkTerminalOnly
#print axioms terminal_body
#print axioms loaded_ids
#print axioms scopes_modules
#print axioms linked_ids
#print axioms terminal_loaded_ids
#print axioms terminal_linked_ids
#print axioms authored_executable_owner
#print axioms full_order_incompatible
#print axioms split_tops
#print axioms split_regions
#print axioms holds_statements
#print axioms run_same

end CoreJS.ProjectedOrder
