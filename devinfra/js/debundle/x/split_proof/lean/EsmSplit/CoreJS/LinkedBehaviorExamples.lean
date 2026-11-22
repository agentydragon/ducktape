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

open Lean (Json)

-- The insertion order and a negative decimal distinguish this literal from a reparse of
-- canonical printed JSON. The string also exercises quotes, escapes and Lean comment syntax.
private def jsonFixture := Json.mkObj [
  ("z", .num ⟨-20, 1⟩), ("a", .arr #[.null, .bool false]),
  ("q", .str "\"\\\n/- -- #eval λ"), ("b", .bool true)]

open Lean Elab in
elab "quotedJsonFixture%" : term => do
  let .ok stx := Parser.runParserCategory (← getEnv) `term (LinkedBehavior.jsonLiteral jsonFixture)
    | throwError "could not parse the fixture AST literal"
  Term.elabTerm stx (some (mkConst ``Json))

theorem jsonLiteralRoundTrip : quotedJsonFixture% = jsonFixture := rfl

private def node (kind : String) (fields : List (String × Json)) : Json :=
  Json.mkObj (("type", .str kind) :: fields)

/-- A proof for a closure returning "first" cannot be reused for a decoded closure returning
"second", even though constructing each closure separately finishes normally. -/
private def closureAST (value : String) : Json :=
  let body := node "Literal" [("value", .str value), ("lit", .str ("s:" ++ value))]
  let arrow := node "ArrowFunctionExpression" [("async", .bool false), ("generator", .bool false),
    ("params", .arr #[]), ("body", body)]
  node "Program" [("sourceType", .str "module"), ("body", .arr #[node "ExpressionStatement" [("expression", arrow)]])]

private def closureProgram : Program :=
  ⟨"main.mjs", [⟨"main.mjs", [], [(0, .expr (.func (.mk "" .arrow false [] [.ret (.str "first")])))], []⟩]⟩

private theorem closureExecution :
    LinkedBehavior.Equivalent closureProgram closureProgram ∧ LinkedBehavior.SameExports closureProgram closureProgram := by
  verify_linked_programs 16

private def checkClosure (value : String) :=
  let source := closureAST value
  let files := Json.mkObj [("main.mjs", source)]
  let plan : EsmSplit.Plan := ⟨"main.mjs", [⟨"main.mjs", [0], [], []⟩]⟩
  (LinkedBehavior.certifyASTs source (Json.mkObj []) files plan closureProgram closureProgram closureExecution).isOk

#guard checkClosure "first"
#guard !checkClosure "second"

end CoreJS.LinkedBehaviorExamples
