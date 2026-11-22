import EsmSplit.CoreJS.LinkedCertificate

namespace CoreJS.LinkedBehaviorExamples

private def provider : Module where
  name := "factory"
  imports := []
  exports := [("fac", "make")]
  body := [(0, .decl .var "make" (.func (.mk "make" .arrow false ["callback"]
    [.ret (.func (.mk "" .arrow false [] [.ret (.call (.var "callback") [])]))])))]

private def callUnit : Nat × Stmt :=
  (1, .decl .var "created" (.call (.var "wrap") [.func (.mk "" .arrow false [] [.ret .undef])]))

private def movedUnit : Nat × Stmt :=
  (2, .decl .const "moved" (.func (.mk "moved" .arrow false [] [.ret .undef])))

private def entry : Module where
  name := "main"
  imports := [⟨"factory", [("fac", "wrap")]⟩]
  exports := [("public", "created")]
  body := [callUnit, movedUnit]

private def source : Program := ⟨"main", [provider, entry]⟩

private def candidate : Program where
  entry := "main"
  modules := [provider, ⟨"split", [], [movedUnit], []⟩,
    { entry with imports := entry.imports ++ [⟨"split", []⟩], body := [callUnit] }]

-- This literal exercises source punctuation, control characters, non-ASCII data, negative zero
-- and a NaN payload. Checking the parsed term against its input also covers the implicit type
-- parameters in lists and pairs produced by Lean's ToExpr instances.
private def literals : Program := Program.single [
  .expr (.str "\"\\\n/- -- #eval λ"), .expr (.num (Float.ofBits 0x8000000000000000)),
  .expr (.num (Float.ofBits 0x7ff8000000000001))]

open Lean Elab in
elab "quotedFixture%" n:ident : term => do
  let p := if n.getId == `source then source else literals
  let .ok text := LinkedBehavior.programLiteral p | throwError "could not quote the fixture"
  let .ok stx := Parser.runParserCategory (← getEnv) `term text | throwError "could not parse the fixture literal"
  Term.elabTerm stx (some (mkConst ``Program))

set_option maxRecDepth 16000
set_option maxHeartbeats 2000000
set_option cbv.warning false

theorem literalRoundTrip : (quotedFixture% source) = source ∧ (quotedFixture% literals) = literals := ⟨rfl, rfl⟩

/-- Moving the independent closure to a dependency evaluates it before the imported
factory call. The public export and the returned closure still refer to the same cells. -/
theorem movedClosure : LinkedBehavior.Equivalent source candidate ∧ LinkedBehavior.SameExports source candidate := by
  verify_linked_programs 16

#audit_linked_programs movedClosure

end CoreJS.LinkedBehaviorExamples
