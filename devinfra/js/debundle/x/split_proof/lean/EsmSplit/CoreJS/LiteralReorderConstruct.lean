import EsmSplit.CoreJS.LiteralReorderExact

/-! A request-derived constructor for one adjacent literal declaration swap. -/

namespace CoreJS.LiteralReorderConstruct

open ExactLiteralMove LiteralReorderLayout LiteralReorderExact

/-- The original CoreJS program, authored request and actual checked plan are
retained in the result. -/
structure Construction (original : Program) (request : EsmSplit.Spec) where
  x : Name
  y : Name
  z : Name
  a : Literal
  b : Literal
  sourceEq : original = sourceProgram request.entry x y z a b
  distinctXY : x ≠ y
  distinctXZ : x ≠ z
  distinctYZ : y ≠ z
  layout : Checked (LiteralReorderLink.bundle x y z) request x

def Construction.program {original request} (c : Construction original request) : Program :=
  splitProgram (LiteralReorderLink.stmts c.x c.y c.z c.a c.b) (LiteralReorderLink.bundle c.x c.y c.z)
    (EsmSplit.mkPlan (LiteralReorderLink.bundle c.x c.y c.z) request)

/-- Read the original units and validate the actual authored request. No source
or candidate program is run, and no replacement request is constructed. -/
def construct (original : Program) (request : EsmSplit.Spec) : Except String (Construction original request) := do
  match originalShape : original with
  | ⟨entry, [⟨moduleName, [], [(0, .decl .const x ex), (1, .decl .const y ey),
      (2, .decl .const z (.var read))], [(exportName, localName)]⟩]⟩ =>
    if sourceNames : entry = request.entry ∧ moduleName = request.entry ∧ read = x ∧
        exportName = z ∧ localName = z then
      let some ⟨a, ha⟩ := LiteralSwapCheck.literal ex | throw "first initializer is not a primitive literal"
      let some ⟨b, hb⟩ := LiteralSwapCheck.literal ey | throw "second initializer is not a primitive literal"
      if distinct : x ≠ y ∧ x ≠ z ∧ y ≠ z then
        let layout ← LiteralReorderLayout.check (LiteralReorderLink.bundle x y z) request x
        have sourceEq :
            (⟨entry, [⟨moduleName, [], [(0, .decl .const x ex), (1, .decl .const y ey),
              (2, .decl .const z (.var read))], [(exportName, localName)]⟩]⟩ : Program) =
                sourceProgram request.entry x y z a b := by
          simp [sourceProgram, sourceModule, decl, sourceNames.1, sourceNames.2.1, sourceNames.2.2.1,
            sourceNames.2.2.2.1, sourceNames.2.2.2.2, ha, hb]
        return ⟨x, y, z, a, b, originalShape.trans sourceEq,
          distinct.1, distinct.2.1, distinct.2.2, layout⟩
      else throw "source declarations must name distinct cells"
    else throw "source entry, read, or public export does not match the supported layout"
  | _ => throw "expected two literal constants and the original exported reader"

/-- Every successful construction preserves the actual authored owners,
complete live export table and exact outcome in every world at every fuel. -/
theorem sound {original request} (c : Construction original request) :
    Places [(0, c.layout.left), (1, c.layout.right), (2, request.entry)] original c.program ∧
    LinkedBehavior.SameExports original c.program ∧
    ∀ w fuel, runProgram w fuel original = runProgram w fuel c.program := by
  have programEq : c.program = splitProgram (LiteralReorderLink.stmts c.x c.y c.z c.a c.b)
      (LiteralReorderLink.bundle c.x c.y c.z) (EsmSplit.mkPlan (LiteralReorderLink.bundle c.x c.y c.z) request) := rfl
  refine ⟨?_, ?_, ?_⟩
  · simpa only [← c.sourceEq, ← programEq] using
      placement request c.x c.y c.z c.a c.b c.layout
  · simpa only [← c.sourceEq, ← programEq] using
      sameExports request c.x c.y c.z c.a c.b c.layout c.distinctXZ c.distinctYZ
  · intro w fuel
    simpa only [← c.sourceEq, ← programEq] using
      outcomes request c.x c.y c.z c.a c.b c.layout c.distinctXY c.distinctXZ c.distinctYZ w fuel

#print axioms construct
#print axioms sound

end CoreJS.LiteralReorderConstruct
