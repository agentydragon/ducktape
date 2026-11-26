import EsmSplit.CoreJS.LinkedCertificate.Proof
import EsmSplit.CoreJS.LinkedCertificate.QuoteCoreJS
import EsmSplit.CoreJS.LinkedCertificate.QuoteJson
import EsmSplit.CoreJS.LinkedPivotCertificate

namespace CoreJS.LinkedBehaviorExamples

deriving instance Lean.ToExpr for EsmSplit.Module
deriving instance Lean.ToExpr for EsmSplit.Plan

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

private def plan : EsmSplit.Plan :=
  ⟨"main", [⟨"factory", [0], [], [("fac", "make")]⟩,
    ⟨"main", [1, 2], [("factory", ["fac"])], [("public", "created")]⟩]⟩

open Lean Elab in
elab "quotedFixture%" n:ident : term <= expectedType => do
  let fixture := match n.getId with
    | `source => toExpr source
    | `plan => toExpr plan
    | _ => toExpr literals
  EsmSplit.CertificateData.elaborate fixture expectedType

set_option maxRecDepth 16000
set_option maxHeartbeats 2000000
set_option cbv.warning false

theorem literalRoundTrip :
    (quotedFixture% source) = source ∧ (quotedFixture% literals) = literals ∧ (quotedFixture% plan) = plan :=
  ⟨rfl, rfl, rfl⟩

-- One module, no imports or exports: simply calling an arrow on initialization used
-- to exhaust the prover when its return expression went through the ordinary unary rule.
private def unaryCall : Program := Program.single [
  .expr (.call (.func (.mk "" .arrow false [] [.ret (.unary .typeof .undef)])) [])]

example (W : World) : LinkedBehavior.normal (runProgram W 16 unaryCall) = true := by cbv

-- The linked reducer must finish a wide dense array even with the ordinary
-- argument recursion hidden from `cbv`.
private def denseArray : Program := Program.single [
  .decl .const "values" (.arr (List.replicate 16 (.arr [.str "item"]))) ]

attribute [local cbv_opaque] evalArgs in
example (W : World) : LinkedBehavior.normal (runProgram W 32 denseArray) = true := by cbv

/-- Moving the independent closure to a dependency evaluates it before the imported
factory call. The public export and the returned closure still refer to the same cells. -/
theorem movedClosure : LinkedBehavior.Equivalent source candidate ∧ LinkedBehavior.SameExports source candidate := by
  verify_linked_programs 16

#audit_linked_programs movedClosure

-- The call moves with the provider import. Its module loses the unrelated
-- binding "moved" and the entry receives "created" through a glue import.
private def ordered : Program where
  entry := "main"
  modules := [provider, ⟨"split", entry.imports, [callUnit], [("created", "created")]⟩,
    { entry with imports := [⟨"split", [("created", "created")]⟩], body := [movedUnit] }]

private noncomputable def sourceLink (w : World) : Option (LinkedBehavior.CheckedLink source w) := by
  check_linked_layout

private noncomputable def orderedLink (w : World) : Option (LinkedBehavior.CheckedLink ordered w) := by
  check_linked_layout

private def premises : HostPremises := {}

theorem orderedClosure :
    LinkedBehavior.EquivalentFor premises source ordered ∧ LinkedBehavior.SameExports source ordered := by
  verify_ordered_linked_programs 16 sourceLink orderedLink premises

#audit_linked_programs orderedClosure

-- An artifact that omits an expected export can fail its syntax check first.
-- Exercise the standalone structural checker on two successfully linked graphs:
-- four distinct public aliases are gone, all pointing at the same original cell.
private def publicAliases (names : List Name) : Program :=
  let m : Module := {
    name := "main"
    imports := []
    body := [(0, .decl .const "item" .undef)]
    exports := names.map fun name => (name, "item")
  }
  ⟨m.name, [m]⟩

#guard match LinkedBehavior.checkStructuralSchedule [] (publicAliases ["a", "b", "c", "d"]) (publicAliases []) {} with
  | .error reason => reason.reason == "entry exports resolve to different linked cells; extra 0; missing 4: " ++
      "\"a\" (original cell @1.0), \"b\" (original cell @1.0), \"c\" (original cell @1.0), …; retargeted 0"
  | .ok _ => false

-- Both graphs link two independent allocations and run them in opposite order.
-- Reading the other allocation by its local binding makes the swap ineligible.
private def allocationPair (value : Expr) (swap : Bool) : Program :=
  let a := (0, Stmt.decl .const "a" (.arr []))
  let b := (1, Stmt.decl .const "b" value)
  ⟨"main", [⟨"main", [], if swap then [b, a] else [a, b], []⟩]⟩

#guard LinkedBehavior.earlierMove? (allocationPair (.arr []) false) (allocationPair (.arr []) true) == some (0, 1)
#guard (LinkedBehavior.earlierMove? (allocationPair (.var "a") false) (allocationPair (.var "a") true)).isNone

private noncomputable def allocationLink (w : World) :
    Option (LinkedBehavior.CheckedLink (allocationPair (.arr []) false) w) := by check_linked_layout

private noncomputable def swappedLink (w : World) :
    Option (LinkedBehavior.CheckedLink (allocationPair (.arr []) true) w) := by check_linked_layout

example (w : World) (final : State w)
    (sourceRun : runProgram w 16 (allocationPair (.arr []) false) = .ok () final) :
    runProgram w 16 (allocationPair (.arr []) true) = .ok () final := by
  apply LinkedBehavior.run_ok_of_moved_link (start := 0) (width := 1)
    ((allocationLink w).get (by rfl)) ((swappedLink w).get (by rfl)) _ _ sourceRun
  · rfl
  · cbv <;> rfl

open Lean (Json)

-- The insertion order and a negative decimal distinguish this literal from a reparse of
-- canonical printed JSON. The string also exercises quotes, escapes and Lean comment syntax.
private def jsonFixture := Json.mkObj [
  ("z", .num ⟨-20, 1⟩), ("a", .arr #[.null, .bool false,
    Json.mkObj [("x", .str "one"), ("y", .num ⟨20, 1⟩)],
    Json.mkObj [("x", .str "two"), ("y", .num ⟨2, 0⟩)],
    Json.mkObj [("y", .str "three"), ("x", .null)]]),
  ("q", .str "\"\\\n/- -- #eval λ"), ("b", .bool true)]

private def wideJsonFixture : Json :=
  Json.mkObj [("body", .arr #[.arr (Array.ofFn fun i : Fin 705 =>
    let key := Json.mkObj [("type", .str "Literal"), ("raw", .str s!"field{i.val}"),
      ("value", .str s!"field{i.val}"), ("lit", .str s!"s:field{i.val}")]
    let value := Json.mkObj [("type", .str "Literal"), ("value", .num ⟨-Int.ofNat i.val, 2⟩)]
    Json.mkObj [("type", .str "Property"), ("key", key),
      ("computed", .bool false), ("method", .bool false), ("shorthand", .bool false),
      ("value", value), ("kind", .str "init")])])]

private def commonJsonFields := #[jsonFixture, .null, .bool false, .str "shared", .num ⟨-3, 1⟩]

private def sourceJsonFixture : Json := .arr (commonJsonFields ++ #[
  Json.mkObj [("z", .bool true), ("a", .null)], .num ⟨1, 1⟩, .null, .null, .null])

private def outputJsonFixture : Json := .arr (commonJsonFields ++ #[
  Json.mkObj [("a", .null), ("z", .bool true)], .num ⟨10, 2⟩, .null, .null, .null])

open Lean Elab Command in
elab "#quoteJsonFixture" name:ident : command => do
  let roots := match name.getId with
    | `quotedWideJsonFixture => [("quotedWideJsonFixture", wideJsonFixture)]
    | `quotedSourceJsonFixture => [("quotedSourceJsonFixture", sourceJsonFixture),
        ("quotedOutputJsonFixture", outputJsonFixture)]
    | _ => [(name.getId.toString, jsonFixture)]
  let (definitions, entries) := LinkedBehavior.jsonDefinitions roots
  modifyEnv (EsmSplit.CertificateData.addEntries · entries)
  for text in definitions do
    let .ok stx := Parser.runParserCategory (← getEnv) `command text
      | throwError "could not parse the quoted JSON definition"
    elabCommand stx

#quoteJsonFixture quotedJsonFixture

theorem jsonLiteralRoundTrip : quotedJsonFixture = jsonFixture := rfl

-- The roots share a chunk. The other chunks preserve distinct raw object trees
-- and mantissa/exponent pairs even when their printed JSON agrees.
#guard sourceJsonFixture.compress == outputJsonFixture.compress
#quoteJsonFixture quotedSourceJsonFixture

theorem sharedJsonLiteralRoundTrip : quotedSourceJsonFixture = sourceJsonFixture ∧
    quotedOutputJsonFixture = outputJsonFixture := ⟨rfl, rfl⟩

-- The recursion budget here applies only to elaborating the generated syntax.
set_option maxRecDepth 512 in
#quoteJsonFixture quotedWideJsonFixture

#guard quotedWideJsonFixture == wideJsonFixture

private def node (kind : String) (fields : List (String × Json)) : Json :=
  Json.mkObj (("type", .str kind) :: fields)

/-- A proof for a closure returning "first" cannot be reused for a decoded closure returning
"second", even though constructing each closure separately finishes normally. -/
private def closureUnit (body : Json) : Json :=
  let arrow := node "ArrowFunctionExpression" [("async", .bool false), ("generator", .bool false),
    ("params", .arr #[]), ("body", body)]
  node "ExpressionStatement" [("expression", arrow)]

private def closureAST (body : Json) : Json :=
  node "Program" [("sourceType", .str "module"), ("body", .arr #[closureUnit body])]

private def stringLiteral (value : String) : Json :=
  node "Literal" [("value", .str value), ("lit", .str ("s:" ++ value))]

private def closureProgram : Program :=
  ⟨"main.mjs", [⟨"main.mjs", [], [(0, .expr (.func (.mk "" .arrow false [] [.ret (.str "first")])))], []⟩]⟩

private theorem closureExecution :
    LinkedBehavior.Equivalent closureProgram closureProgram ∧ LinkedBehavior.SameExports closureProgram closureProgram := by
  verify_linked_programs 16

private def checkClosure (body : Json) (p : Program)
    (semantics : LinkedBehavior.Equivalent p p ∧ LinkedBehavior.SameExports p p) : Bool :=
  let source := closureAST body
  let files := Json.mkObj [("main.mjs", source)]
  let plan : EsmSplit.Plan := ⟨"main.mjs", [⟨"main.mjs", [0], [], []⟩]⟩
  (LinkedBehavior.certifyASTs .empty {} source (Json.mkObj []) files plan p p semantics).isOk

#guard checkClosure (stringLiteral "first") closureProgram closureExecution
#guard !checkClosure (stringLiteral "second") closureProgram closureExecution

private def spread (operand : Json) : Json := node "SpreadElement" [("argument", operand)]

private def arrayLiteral (elements : Array Json) : Json := node "ArrayExpression" [("elements", .arr elements)]

private def spreadProgram : Program :=
  let body : Expr := .arr [.spreadArg (.str "first"), .spreadArg (.arr [])]
  ⟨"main.mjs", [⟨"main.mjs", [], [(0, .expr (.func (.mk "" .arrow false [] [.ret body])))], []⟩]⟩

private theorem spreadExecution :
    LinkedBehavior.Equivalent spreadProgram spreadProgram ∧ LinkedBehavior.SameExports spreadProgram spreadProgram := by
  verify_linked_programs 16

private def checkSpread (first : Json) : Bool :=
  checkClosure (arrayLiteral #[first, spread (arrayLiteral #[])]) spreadProgram spreadExecution

-- Mutate the tag of one element while keeping the other spread and the whole array.
#guard checkSpread (spread (stringLiteral "first"))
#guard !checkSpread (stringLiteral "first")
#guard !checkSpread (spread (stringLiteral "second"))

private def imported (name : String) : Json := node "Identifier" [("name", .str name), ("k", .str "V")]

-- The captures come from every element, including those with a spread tag.
#guard
  ((FromESTree.linkedUnit ["direct", "iterable"] (closureUnit
    (arrayLiteral #[imported "direct", spread (imported "iterable")]))).map Stmt.free).toOption ==
    some ["direct", "iterable"]

private def objectSpreads (operands : Array Json) : Json :=
  node "ObjectExpression" [("properties", .arr (operands.map spread))]

private def objectSpreadProgram : Program :=
  let body : Expr := .obj [.spread (.str "first"), .spread (.str "second")]
  ⟨"main.mjs", [⟨"main.mjs", [], [(0, .expr (.func (.mk "" .arrow false [] [.ret body])))], []⟩]⟩

private theorem objectSpreadExecution : LinkedBehavior.Equivalent objectSpreadProgram objectSpreadProgram ∧
    LinkedBehavior.SameExports objectSpreadProgram objectSpreadProgram := by
  verify_linked_programs 16

#guard checkClosure (objectSpreads #[stringLiteral "first", stringLiteral "second"]) objectSpreadProgram objectSpreadExecution
#guard !checkClosure (objectSpreads #[stringLiteral "second", stringLiteral "first"]) objectSpreadProgram objectSpreadExecution
#guard !checkClosure (objectSpreads #[stringLiteral "first"]) objectSpreadProgram objectSpreadExecution

#guard
  ((FromESTree.linkedUnit ["source"] (closureUnit (objectSpreads #[imported "source"]))).map Stmt.free).toOption ==
    some ["source"]

-- Inspect the CoreJS heap as well as checking the emitted JavaScript in Node.
private def asyncMethodShape : M World.probe Bool := do
  let fn ← makeFunc ⟨[], .undef, none⟩ (.opaque "read" .asyncFn false 1 0 1 [] (some "deferred"))
  let .obj proto ← prim (callFn 8) .getPrototypeOf [fn] | return false
  let .obj parent ← prim (callFn 8) .getPrototypeOf [.obj proto] | return false
  let .str name ← getProp (callFn 8) fn (.str "name") | return false
  let .num arity ← getProp (callFn 8) fn (.str "length") | return false
  let hasPrototype ← hasProp (.str "prototype") fn
  return proto == asyncFunctionProto && parent == functionProto && proto != parent &&
    name == "read" && arity == 1 && !(← isConstructor fn) && (hasPrototype matches .bool false)

#guard match asyncMethodShape (initialState World.probe 1) with
  | .ok result _ => result
  | _ => false

private def changedSuperclass (inPrefix : Bool) : Program :=
  let superclass (name marker : String) : Stmt :=
    .decl .const name (.cls name false (.mk name .classCtor false []
      [.expr (.prim .log [.str marker])]) [])
  let change := Expr.prim .setPrototypeOf [.var "Child", .var "Alternate"]
  let argument := Expr.prim .log [.str "argument"]
  let before := if inPrefix then [Stmt.expr change] else []
  let args := [if inPrefix then argument else Expr.seq change argument]
  let tail := Func.mk "Child" .classCtor false [] []
  .single [
    superclass "Base" "base",
    superclass "Alternate" "alternate",
    .decl .const "Child" (.derived "Child" false (.var "Base") [] (some (before, .explicit args, tail))),
    .expr (.new (.var "Child") [])
  ]

-- A superclass change during argument evaluation stops the checker before invoking either constructor.
#guard match runProgram World.probe 96 (changedSuperclass false) with
  | .unknown reason state =>
    reason == "superclass changed while evaluating super arguments" && state.out == #["argument"]
  | _ => false

#guard match runProgram World.probe 96 (changedSuperclass true) with
  | .ok _ state => state.out == #["argument", "alternate"]
  | _ => false

-- A fresh arguments object has its own intrinsic iterator, but the next method on the
-- resulting iterator's prototype remains mutable. The forwarding shortcut must check it.
private def modifiedIterator : M World.probe (List Val) := do
  modifyObj arrayIteratorProto fun proto => { proto with props := [] }
  forwardFreshArguments [.num 7]

#guard match modifiedIterator (initialState World.probe 1) with
  | .unknown reason _ => reason == "arguments spread uses a modified array iterator"
  | _ => false

private def changedMethodPrototype : Program :=
  let read := Expr.callMethod (.var "borrower") (.str "callback") []
  .single [
    .decl .const "home" (.obj [
      .proto (.obj [.init (.str "value") (.str "original")]),
      .method (.str "callback") (.mk "callback" .method false [] [
        .ret (.func (.mk "" .arrow false [] [.ret (.getSuper (.str "value"))]))])]),
    .decl .const "borrower" (.obj [
      .init (.str "callback") (.get (.var "home") (.str "callback"))]),
    .decl .const "later" read,
    .expr (.prim .log [.call (.var "later") []]),
    .expr (.prim .setPrototypeOf [.var "home", .obj [.init (.str "value") (.str "updated")]]),
    .expr (.prim .log [.call (.var "later") []])
  ]

-- The arrow keeps the lexical home even after the method is borrowed. A later call
-- reads that object's current prototype, so only the second result sees the update.
#guard match runProgram World.probe 96 changedMethodPrototype with
  | .ok _ state => state.out == #["original", "updated"]
  | _ => false

end CoreJS.LinkedBehaviorExamples
