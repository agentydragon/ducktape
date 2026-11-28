import EsmSplit.CoreJS.LiteralReorderGraph

/-! Exact outcomes and entry cells for the request-derived literal reorder. -/

namespace CoreJS.LiteralReorderExact

open ExactLiteralMove LiteralReorderLayout LiteralReorderGraph

private def leftEnv (x : Name) : Env :=
  ⟨[(x, ⟨1, 0⟩)], .undef, none⟩

private def rightEnv (y : Name) : Env :=
  ⟨[(y, ⟨2, 0⟩)], .undef, none⟩

private def entryEnv (x z : Name) : Env :=
  ⟨[(z, ⟨3, 0⟩), (x, ⟨1, 0⟩)], .undef, none⟩

private theorem leftAgree (x y z : Name) (a : Literal) :
    Agree (decl x a).free (sourceEnv x y z) (leftEnv x) := by
  refine ⟨rfl, rfl, ?_⟩
  simp [decl, Stmt.free, literal_closed, sourceEnv, leftEnv] <;> decide

private theorem rightAgree (x y z : Name) (b : Literal) (hxy : x ≠ y) :
    Agree (decl y b).free (sourceEnv x y z) (rightEnv y) := by
  refine ⟨rfl, rfl, ?_⟩
  simp [decl, Stmt.free, literal_closed, sourceEnv, rightEnv, List.lookup_cons,
    beq_eq_false_iff_ne.mpr (Ne.symm hxy)]

private theorem entryAgree (x y z : Name) (hxz : x ≠ z) (hyz : y ≠ z) :
    Agree (Stmt.decl .const z (.var x)).free (sourceEnv x y z) (entryEnv x z) := by
  refine ⟨rfl, rfl, ?_⟩
  simp [Stmt.free, Expr.free, sourceEnv, entryEnv, List.lookup_cons,
    beq_eq_false_iff_ne.mpr (Ne.symm hxz), beq_eq_false_iff_ne.mpr (Ne.symm hyz),
    beq_eq_false_iff_ne.mpr hxz]

/-- The actual planner's program preserves every bounded modeled outcome. -/
theorem outcomes (S : EsmSplit.Spec) (x y z : String) (a b : Literal)
    (c : Checked (LiteralReorderLink.bundle x y z) S x) (hxy : x ≠ y) (hxz : x ≠ z) (hyz : y ≠ z)
    (w : World) (fuel : Nat) :
    runProgram w fuel (sourceProgram S.entry x y z a b) =
      runProgram w fuel
        (splitProgram (LiteralReorderLink.stmts x y z a b) (LiteralReorderLink.bundle x y z)
          (EsmSplit.mkPlan (LiteralReorderLink.bundle x y z) S)) := by
  rw [graph_of_checked S x y z a b c]
  rw [LinkedBehavior.run_after_link fuel (sourceLink w S.entry x y z a b),
    LinkedBehavior.run_after_link fuel (graphLink S x y z a b c w)]
  have schedule :
    SafeReorder (unitsOf (sourceLink w S.entry x y z a b).linked)
      [(1, decl y b, sourceEnv x y z), (0, decl x a, sourceEnv x y z),
        (2, .decl .const z (.var x), sourceEnv x y z)] := by
    apply
      SafeReorder.swap [] [(2, .decl .const z (.var x), sourceEnv x y z)] 0 1 (sourceEnv x y z)
        (sourceEnv x y z) x y a b ⟨1, 0⟩ ⟨2, 0⟩
    · simp [sourceEnv]
    · simp [sourceEnv, List.lookup_cons, beq_eq_false_iff_ne.mpr (Ne.symm hxy)]
    · decide
  have scopes :
    SameUnits
      [(1, decl y b, sourceEnv x y z), (0, decl x a, sourceEnv x y z),
        (2, .decl .const z (.var x), sourceEnv x y z)]
      (unitsOf (graphLink S x y z a b c w).linked) := by
    exact
      .cons ⟨rfl, rfl, rightAgree x y z b hxy⟩
        (.cons ⟨rfl, rfl, leftAgree x y z a⟩ (.cons ⟨rfl, rfl, entryAgree x y z hxz hyz⟩ .nil))
  exact
    (reorder_exact fuel schedule (sourceLink w S.entry x y z a b).ready).trans
      (congrFun (runUnits_same fuel scopes) (sourceLink w S.entry x y z a b).ready)

/-- The complete public table resolves to the same original live cell. -/
theorem sameExports (S : EsmSplit.Spec) (x y z : String) (a b : Literal)
    (c : Checked (LiteralReorderLink.bundle x y z) S x) (hxz : x ≠ z) (hyz : y ≠ z) :
    LinkedBehavior.SameExports (sourceProgram S.entry x y z a b)
      (splitProgram (LiteralReorderLink.stmts x y z a b) (LiteralReorderLink.bundle x y z)
        (EsmSplit.mkPlan (LiteralReorderLink.bundle x y z) S)) := by
  rw [graph_of_checked S x y z a b c]
  intro w
  refine ⟨[(z, ⟨3, 0⟩)], [(z, ⟨3, 0⟩)], ?_, ?_, List.Perm.refl _⟩
  · unfold LinkedBehavior.exports
    rw [(sourceLink w S.entry x y z a b).equation]
    change (EntryExports.resolve S.entry (sourceLinked S.entry x y z a b)).toOption = _
    simp [EntryExports.resolve, sourceLinked, sourceModule, sourceEnv, List.lookup_cons,
      Except.toOption, beq_eq_false_iff_ne.mpr (Ne.symm hxz), beq_eq_false_iff_ne.mpr (Ne.symm hyz)]
    rfl
  · have notEntry : S.entry ∉ [c.left, c.right] := (List.nodup_cons.mp c.distinct).1
    have el : S.entry ≠ c.left := fun h => notEntry (by simp [h])
    have er : S.entry ≠ c.right := fun h => notEntry (by simp [h])
    unfold LinkedBehavior.exports
    rw [(graphLink S x y z a b c w).equation]
    change (EntryExports.resolve S.entry (linked S.entry c.left c.right x y z a b)).toOption = _
    simp [EntryExports.resolve, linked, List.lookup_cons, Except.toOption,
      beq_eq_false_iff_ne.mpr (Ne.symm er), beq_eq_false_iff_ne.mpr (Ne.symm el)]
    rfl

/-- Every original executable unit is placed in its actual authored owner. -/
theorem placement (S : EsmSplit.Spec) (x y z : String) (a b : Literal)
    (c : Checked (LiteralReorderLink.bundle x y z) S x) :
    Places [(0, c.left), (1, c.right), (2, S.entry)] (sourceProgram S.entry x y z a b)
      (splitProgram (LiteralReorderLink.stmts x y z a b) (LiteralReorderLink.bundle x y z)
        (EsmSplit.mkPlan (LiteralReorderLink.bundle x y z) S)) := by
  have safe := c.safe
  simp only [List.all_cons, List.all_nil, Bool.and_eq_true, and_true] at safe
  obtain ⟨he, hl, hr⟩ := safe
  rw [graph_of_checked S x y z a b c]
  unfold Places
  constructor
  · change ([0, 1, 2] : List Nat).Nodup
    decide
  · constructor
    · simp [Program.fileUnitPlacements, sourceProgram, sourceModule, Module.unitPlacements, he]
    · simp [Program.fileUnitPlacements, graph, Module.unitPlacements, he, hl, hr]
      exact
        (show
              [(2, S.entry), (0, c.left), (1, c.right)].Perm
                [(0, c.left), (2, S.entry), (1, c.right)]
              from .swap _ _ _).trans
          (.cons _
            (show [(2, S.entry), (1, c.right)].Perm [(1, c.right), (2, S.entry)] from .swap _ _ _))

end CoreJS.LiteralReorderExact
