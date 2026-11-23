import EsmSplit.CoreJS.FromESTree
import EsmSplit.Lower

/-! Build a top-level opaque statement for structural proofs from a parsed, scope-annotated source unit.
The parser and scope resolver supply identifier classifications. The caller must bind the code ID
to the same original source unit on both sides of a move and check its captures in both linked scopes. -/

namespace CoreJS.OpaqueInput

open Lean (Json)

private structure Names where
  declared : Std.HashSet Name := {}
  references : Std.HashSet Name := {}
  deriving Inhabited

/-- Property spellings and labels have no scope annotation. Module variables and imported references
need actual binding cells. Ambient references stay in the code the world interprets. -/
private partial def names (j : Json) : StateT Names (Except String) Unit := do
  if Lower.typ j == "Identifier" then
    let cls ← match j.getObjVal? "k" with
      | .error _ => pure ""
      | .ok c => do
        let cls ← c.getStr?
        unless ["T", "V", "L", "G"].contains cls do throw "unknown identifier scope annotation"
        pure cls
    let declared ← match j.getObjVal? "d" with
      | .error _ => pure false
      | .ok d => do
        unless d == .num 1 do throw "invalid module declaration annotation"
        pure true
    if declared && cls != "T" then throw "module declaration lacks a module binding annotation"
    if declared || cls == "T" || cls == "V" then
      let name ← FromESTree.identifier j
      if declared then modify fun s => { s with declared := s.declared.insert name }
      else modify fun s => { s with references := s.references.insert name }
  else
    for child in Lower.children j do names child

/-- Link ordinary function declarations with the arity visible on the function object.
Async and generator functions also expose distinct intrinsic prototypes. -/
private def hoisted (j : Json) : Except String (Name × Nat × Bool) := do
  let name ← j.getObjVal? "id" >>= FromESTree.moduleBinding
  if (← j.getObjValAs? Bool "async") || (← j.getObjValAs? Bool "generator") then
    throw "unsupported opaque async or generator function declaration"
  let params ← j.getObjValAs? (List Json) "params"
  let arity := (params.takeWhile fun p => !["AssignmentPattern", "RestElement"].contains (Lower.typ p)).length
  return (name, arity, true)

/-- Lower executable module-body units. Static import and export syntax belongs to the module linker.
The traversal includes references from deferred code, even when the initial world step never calls it. -/
def unit (origin : Nat) (j : Json) : Except String Stmt := do
  if let some why := Lower.outsideModel false j then throw why
  if FromESTree.needsOpaqueContext j then throw "opaque unit requires source-module or method context"
  let kind ← j.getObjValAs? String "type"
  unless ["VariableDeclaration", "FunctionDeclaration", "ClassDeclaration", "ExpressionStatement",
      "BlockStatement", "TryStatement", "IfStatement", "ForStatement", "ForInStatement", "ForOfStatement",
      "WhileStatement", "DoWhileStatement", "SwitchStatement", "LabeledStatement", "ThrowStatement",
      "DebuggerStatement", "EmptyStatement"].contains kind do throw s!"unsupported opaque module statement {kind}"
  let (_, scope) ← (names j).run {}
  let declared := (Lower.topDefs j).eraseDups
  unless declared.Perm scope.declared.toList do throw "opaque declarations differ from module scope annotations"
  -- Request slots must be stable across reparsing and JSON object-field order.
  let free := (scope.references.toList.filter fun x => !scope.declared.contains x).mergeSort
  if kind == "FunctionDeclaration" then
    return .opaque origin [] [← hoisted j] free (.closures [])
  let declKind ← if kind == "VariableDeclaration" then
    match ← j.getObjValAs? String "kind" with
    | "const" => pure DeclKind.const
    | "let" => pure .let_
    | "var" => pure .var
    | other => throw s!"unsupported opaque declaration kind {other}"
  else pure (if kind == "ClassDeclaration" then .let_ else .var)
  return .opaque origin (declared.map (·, declKind)) [] free .world

end CoreJS.OpaqueInput
