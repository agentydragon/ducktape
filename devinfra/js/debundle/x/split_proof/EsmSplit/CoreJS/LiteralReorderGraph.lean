import EsmSplit.CoreJS.LiteralReorderLayout

/-! The actual split graph for the request-derived three-unit layout. -/

namespace CoreJS.LiteralReorderGraph

open ExactLiteralMove

/-- Explicit graph corresponding to the checked planner layout. Its paths
are supplied by the actual request. -/
def graph (entry left right x y z : String) (a b : Literal) : Program :=
  ⟨entry, [
    ⟨entry, [⟨right, []⟩, ⟨left, [(x, x)]⟩], [(2, .decl .const z (.var x))], [(z, z)]⟩,
    ⟨left, [], [(0, decl x a)], [(x, x)]⟩,
    ⟨right, [], [(1, decl y b)], []⟩]⟩

/-- The graph is the actual `splitProgram` of the checked planner, for any
three pairwise-distinct module paths. -/
theorem graph_of_plan (entry left right x y z : String) (a b : Literal)
    (el : entry ≠ left) (er : entry ≠ right) (lr : left ≠ right) :
    splitProgram (LiteralReorderLink.stmts x y z a b) (LiteralReorderLink.bundle x y z)
      (LiteralReorderLayout.expectedPlan entry left right x) = graph entry left right x y z a b := by
  simp [splitProgram, moduleOf, LiteralReorderLayout.expectedPlan, LiteralReorderLink.stmts,
    LiteralReorderLink.bundle, graph, EsmSplit.Plan.namedExports, EsmSplit.Plan.find,
    EsmSplit.Module.namedExports, EsmSplit.Module.namedExportsFrom, EsmSplit.Module.publishedFrom,
    EsmSplit.localExports, EsmSplit.exportNameFor, el, er, lr]

/-- Apply the graph lemma to the owners and plan obtained from the actual
request, without supplying or changing the request's module names. -/
theorem graph_of_checked (S : EsmSplit.Spec) (x y z : String) (a b : Literal)
    (c : LiteralReorderLayout.Checked (LiteralReorderLink.bundle x y z) S x) :
    splitProgram (LiteralReorderLink.stmts x y z a b) (LiteralReorderLink.bundle x y z)
      (EsmSplit.mkPlan (LiteralReorderLink.bundle x y z) S) =
        graph S.entry c.left c.right x y z a b := by
  have notEntry : S.entry ∉ [c.left, c.right] := (List.nodup_cons.mp c.distinct).1
  have notLeft : c.left ∉ [c.right] := (List.nodup_cons.mp (List.nodup_cons.mp c.distinct).2).1
  have el : S.entry ≠ c.left := fun h => notEntry (by simp [h])
  have er : S.entry ≠ c.right := fun h => notEntry (by simp [h])
  have lr : c.left ≠ c.right := fun h => notLeft (by simp [h])
  rw [c.plan]
  exact graph_of_plan S.entry c.left c.right x y z a b el er lr

/-- The actual authored priority gives the graph's evaluation order. -/
theorem graph_order (S : EsmSplit.Spec) (x y z : String) (a b : Literal)
    (c : LiteralReorderLayout.Checked (LiteralReorderLink.bundle x y z) S x) :
    (graph S.entry c.left c.right x y z a b).evalOrder = [c.right, c.left, S.entry] := by
  rw [← graph_of_checked S x y z a b c, split_evalOrder]
  exact c.evaluation

/-- The actual graph loads its modules in the checked request's order. -/
theorem graph_loaded (S : EsmSplit.Spec) (x y z : String) (a b : Literal)
    (c : LiteralReorderLayout.Checked (LiteralReorderLink.bundle x y z) S x) :
    (graph S.entry c.left c.right x y z a b).loaded = [
      ⟨c.right, [], [(1, decl y b)], []⟩,
      ⟨c.left, [], [(0, decl x a)], [(x, x)]⟩,
      ⟨S.entry, [⟨c.right, []⟩, ⟨c.left, [(x, x)]⟩],
        [(2, .decl .const z (.var x))], [(z, z)]⟩] := by
  have notEntry : S.entry ∉ [c.left, c.right] := (List.nodup_cons.mp c.distinct).1
  have notLeft : c.left ∉ [c.right] := (List.nodup_cons.mp (List.nodup_cons.mp c.distinct).2).1
  have el : S.entry ≠ c.left := fun h => notEntry (by simp [h])
  have er : S.entry ≠ c.right := fun h => notEntry (by simp [h])
  have lr : c.left ≠ c.right := fun h => notLeft (by simp [h])
  unfold Program.loaded
  rw [graph_order S x y z a b c]
  simp [Program.find, graph,
    beq_eq_false_iff_ne.mpr el, beq_eq_false_iff_ne.mpr er, beq_eq_false_iff_ne.mpr lr]

/-- The cells allocated for the three original statement identities. -/
def frames (x y z : Name) : List (Nat × List (Name × Addr)) :=
  [(0, [(x, ⟨1, 0⟩)]), (1, [(y, ⟨2, 0⟩)]), (2, [(z, ⟨3, 0⟩)])]

/-- The generated import reaches the original declaration's live cell for
arbitrary distinct entry and owner paths. -/
theorem import_left (entry left right x y z : String) (a b : Literal) (el : entry ≠ left) :
    (graph entry left right x y z a b).importCell (frames x y z) left x = .ok ⟨1, 0⟩ := by
  apply Program.importCell_local (x := x) (m := ⟨left, [], [(0, decl x a)], [(x, x)]⟩)
  · simp [Program.find, graph, beq_eq_false_iff_ne.mpr el]
  · simp
  · simp [Module.scope, frames, List.lookup_cons]

private theorem find_left (entry left right x y z : String) (a b : Literal) (el : entry ≠ left) :
    (graph entry left right x y z a b).find left = some ⟨left, [], [(0, decl x a)], [(x, x)]⟩ := by
  simp [Program.find, graph, beq_eq_false_iff_ne.mpr el]

private theorem find_right (entry left right x y z : String) (a b : Literal)
    (er : entry ≠ right) (lr : left ≠ right) :
    (graph entry left right x y z a b).find right = some ⟨right, [], [(1, decl y b)], []⟩ := by
  simp [Program.find, graph, beq_eq_false_iff_ne.mpr er, beq_eq_false_iff_ne.mpr lr]

/-- Both real entry imports resolve, and the only imported binding is the
original left declaration's cell. -/
theorem entry_imports (entry left right x y z : String) (a b : Literal)
    (el : entry ≠ left) (er : entry ≠ right) (lr : left ≠ right) :
    (graph entry left right x y z a b).imported (frames x y z)
      [⟨right, []⟩, ⟨left, [(x, x)]⟩] = .ok [(x, ⟨1, 0⟩)] := by
  simp [Program.imported, Program.importCells, find_left entry left right x y z a b el,
    find_right entry left right x y z a b er lr, import_left entry left right x y z a b el]
  rfl

/-- Linked modules in the checked execution order, with the shared cells. -/
def linked (entry left right x y z : String) (a b : Literal) : List (Module × Env) := [
  (⟨right, [], [(1, decl y b)], []⟩, ⟨[(y, ⟨2, 0⟩)], .undef, none⟩),
  (⟨left, [], [(0, decl x a)], [(x, x)]⟩, ⟨[(x, ⟨1, 0⟩)], .undef, none⟩),
  (⟨entry, [⟨right, []⟩, ⟨left, [(x, x)]⟩], [(2, .decl .const z (.var x))], [(z, z)]⟩,
    ⟨[(z, ⟨3, 0⟩), (x, ⟨1, 0⟩)], .undef, none⟩)]

/-- The graph's real scope construction installs the original cells. -/
theorem graph_scopes (S : EsmSplit.Spec) (x y z : String) (a b : Literal)
    (c : LiteralReorderLayout.Checked (LiteralReorderLink.bundle x y z) S x) :
    (graph S.entry c.left c.right x y z a b).scopes (frames x y z)
      (graph S.entry c.left c.right x y z a b).loaded =
        .ok (linked S.entry c.left c.right x y z a b) := by
  have notEntry : S.entry ∉ [c.left, c.right] := (List.nodup_cons.mp c.distinct).1
  have notLeft : c.left ∉ [c.right] := (List.nodup_cons.mp (List.nodup_cons.mp c.distinct).2).1
  have el : S.entry ≠ c.left := fun h => notEntry (by simp [h])
  have er : S.entry ≠ c.right := fun h => notEntry (by simp [h])
  have lr : c.left ≠ c.right := fun h => notLeft (by simp [h])
  have cell := import_left S.entry c.left c.right x y z a b el
  unfold frames at cell
  rw [graph_loaded S x y z a b c]
  simp [Program.scopes, Program.imported, Program.importCells,
    find_left S.entry c.left c.right x y z a b el,
    find_right S.entry c.left c.right x y z a b er lr,
    Module.scope, frames, linked, List.lookup_cons, cell]
  rfl

/-- The checked plan executes exactly the original identities once. -/
theorem execution_perm (S : EsmSplit.Spec) (x y z : String)
    (a b : Literal) (c : LiteralReorderLayout.Checked (LiteralReorderLink.bundle x y z) S x) :
    (EsmSplit.mkPlan (LiteralReorderLink.bundle x y z) S).execOrder.Perm
      (List.range (LiteralReorderLink.stmts x y z a b).length) := by
  rw [c.execution]
  change ([1, 0, 2] : List Nat).Perm (List.range 3)
  decide

/-- Link allocation is ordered by the original identities, not module order. -/
theorem graph_tops (S : EsmSplit.Spec) (x y z : String) (a b : Literal)
    (c : LiteralReorderLayout.Checked (LiteralReorderLink.bundle x y z) S x) :
    (graph S.entry c.left c.right x y z a b).tops =
      [(0, decl x a), (1, decl y b), (2, .decl .const z (.var x))] := by
  rw [← graph_of_checked S x y z a b c]
  have h := split_tops (LiteralReorderLink.stmts x y z a b) (LiteralReorderLink.bundle x y z)
    (EsmSplit.mkPlan (LiteralReorderLink.bundle x y z) S) (execution_perm S x y z a b c)
  have range : List.range 3 = [0, 1, 2] := by decide
  simpa [LiteralReorderLink.stmts, range] using h

/-- Every original identity has its original allocation region. -/
theorem graph_regions (S : EsmSplit.Spec) (x y z : String) (a b : Literal)
    (c : LiteralReorderLayout.Checked (LiteralReorderLink.bundle x y z) S x) :
    (graph S.entry c.left c.right x y z a b).regions = 3 := by
  rw [← graph_of_checked S x y z a b c]
  simpa [LiteralReorderLink.stmts] using
    split_regions (LiteralReorderLink.stmts x y z a b) (LiteralReorderLink.bundle x y z)
      (EsmSplit.mkPlan (LiteralReorderLink.bundle x y z) S) (execution_perm S x y z a b c)

private def ready (w : World) : State w := LiteralReorderLayout.ready w

private theorem init_consts {W : World} (units : List LinkedUnit)
    (consts : ∀ u ∈ units, ∃ name value, u.stmt = .decl .const name value) :
    (initFunctions units : M W Unit) = pure () := by
  induction units with
  | nil => rfl
  | cons u rest ih =>
    obtain ⟨id, statement, env⟩ := u
    obtain ⟨name, value, eq⟩ := consts (id, statement, env) (by simp)
    change statement = .decl .const name value at eq
    subst statement
    simp only [initFunctions, initFunction, M.pure_bind,
      ih (fun u hu => consts u (List.mem_cons_of_mem _ hu))]

/-- The actual graph links in every world to the source's allocation state. -/
def graphLink (S : EsmSplit.Spec) (x y z : String) (a b : Literal)
    (c : LiteralReorderLayout.Checked (LiteralReorderLink.bundle x y z) S x) (w : World) :
    LinkedBehavior.CheckedLink (graph S.entry c.left c.right x y z a b) w :=
  ⟨linked S.entry c.left c.right x y z a b, ready w, by
    have allocated : declareTops [(0, decl x a), (1, decl y b), (2, .decl .const z (.var x))]
        (initialState w 3) = .ok (frames x y z) (ready w) := by cbv
    have identities : (checkIdentities [(0, decl x a), (1, decl y b),
        (2, .decl .const z (.var x))] : M w Unit) = pure () := by cbv
    have hoists : (initFunctions ((unitsOf (linked S.entry c.left c.right x y z a b)).mergeSort
        fun a b => decide (a.id ≤ b.id)) : M w Unit) = pure () := by
      apply init_consts
      intro u hu
      have member := (List.mergeSort_perm (unitsOf (linked S.entry c.left c.right x y z a b)) _).mem_iff.mp hu
      simp [unitsOf, linked] at member
      rcases member with rfl | rfl | rfl <;> exact ⟨_, _, rfl⟩
    unfold link linkUsingScopes
    rw [graph_tops S x y z a b c, graph_regions S x y z a b c, identities, M.pure_bind,
      M.bind_apply, allocated]
    change (lift ((graph S.entry c.left c.right x y z a b).scopes (frames x y z)
        (graph S.entry c.left c.right x y z a b).loaded) >>= fun loaded => do
          initFunctions ((unitsOf loaded).mergeSort fun a b => decide (a.id ≤ b.id))
          pure loaded) (ready w) = _
    rw [graph_scopes S x y z a b c, lift, M.pure_bind, hoists, M.pure_bind] <;> rfl⟩


end CoreJS.LiteralReorderGraph
