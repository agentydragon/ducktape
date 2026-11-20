import Lean.Data.Json
import Std.Data.HashSet

/-!
# Footprints from raw ESTree, in Lean

The lowering is `Lean.Json`: acorn's ESTree, untyped, so every node type of JavaScript is
representable. Several syntax traversals are partial Lean definitions; their
termination and correspondence to JavaScript are not kernel-proved.

Soundness is by construction and monotone:

* A name is treated as local only if a *recognized* binder introduces it (parameter, `let`/`const`/
  `var`/function/class declaration, `catch`, class or function-expression name).
* Any node type not recognized goes through the generic fallback: every identifier below it is a
  reference. That can add dependencies, never remove one.
* A statement is inert only if its shape is in a short allow-list. Everything else is an effect.

So adding precision (recognizing a binder, extending the allow-list) is the only way to get
anything wrong. `Experiments/lowering/compare.mjs` checks it against `eslint-scope`.
-/

open Lean

namespace Lower

structure Acc where
  eager    : Array String := #[]
  lazy     : Array String := #[]
  /-- non-local names this statement assigns to (not declares) while it runs -/
  assignedEager : Array String := #[]
  /-- non-local names assigned inside the function bodies it creates, i.e. when they are called -/
  assignedLazy  : Array String := #[]
  /-- node types handled by the generic fallback -/
  fallback : Array String := #[]

abbrev M := StateM Acc

def typ (j : Json) : String := (j.getObjValD "type").getStr?.toOption.getD ""
def fld (j : Json) (k : String) : Json := j.getObjValD k
def items (j : Json) : Array Json := j.getArr?.toOption.getD #[]
def str (j : Json) : String := j.getStr?.toOption.getD ""
def flag (j : Json) (k : String) : Bool := (j.getObjValD k).getBool?.toOption.getD false
def isNull : Json → Bool | .null => true | _ => false

def children : Json → Array Json
  | .obj kvs => kvs.valuesArray
  | .arr a => a
  | _ => #[]

/-- Names a pattern binds. -/
partial def binders (j : Json) : List String :=
  match typ j with
  | "Identifier" => [str (fld j "name")]
  | "ObjectPattern" => (items (fld j "properties")).toList.flatMap fun p =>
      if typ p == "RestElement" then binders (fld p "argument") else binders (fld p "value")
  | "ArrayPattern" => (items (fld j "elements")).toList.flatMap binders
  | "AssignmentPattern" => binders (fld j "left")
  | "RestElement" => binders (fld j "argument")
  | _ => []

/-- `let`/`const`/`class`/`function` declared directly in a statement list (block scope). -/
def lexicalNames (stmts : Array Json) : List String :=
  stmts.toList.flatMap fun s =>
    match typ s with
    | "VariableDeclaration" =>
      if str (fld s "kind") == "var" then [] else
        (items (fld s "declarations")).toList.flatMap fun d => binders (fld d "id")
    | "FunctionDeclaration" | "ClassDeclaration" => binders (fld s "id")
    | _ => []

/-- `var` names hoisted to the enclosing function: through blocks, not through functions. -/
partial def varNames (s : Json) : List String :=
  match typ s with
  | "VariableDeclaration" =>
    if str (fld s "kind") == "var" then (items (fld s "declarations")).toList.flatMap fun d => binders (fld d "id") else []
  | "BlockStatement" => (items (fld s "body")).toList.flatMap varNames
  | "IfStatement" => varNames (fld s "consequent") ++ varNames (fld s "alternate")
  | "ForStatement" => varNames (fld s "init") ++ varNames (fld s "body")
  | "ForInStatement" | "ForOfStatement" => varNames (fld s "left") ++ varNames (fld s "body")
  | "WhileStatement" | "DoWhileStatement" | "LabeledStatement" => varNames (fld s "body")
  | "TryStatement" =>
    varNames (fld s "block") ++ varNames (fld (fld s "handler") "body") ++ varNames (fld s "finalizer")
  | "SwitchStatement" =>
    (items (fld s "cases")).toList.flatMap fun c => (items (fld c "consequent")).toList.flatMap varNames
  | _ => []

def ref (bound : List String) (inFn : Bool) (n : String) : M Unit :=
  unless bound.contains n do
    modify fun s => if inFn then { s with lazy := s.lazy.push n } else { s with eager := s.eager.push n }

def noteFallback (t : String) : M Unit := modify fun s => { s with fallback := s.fallback.push t }

mutual

/-- Visit a node. `bound`: names bound locally. `inFn`: inside a function body (reads are lazy). -/
partial def visit (bound : List String) (inFn : Bool) (j : Json) : M Unit := do
  match typ j with
  | "" => for c in children j do visit bound inFn c
  | "Identifier" => ref bound inFn (str (fld j "name"))
  | "Literal" | "ThisExpression" | "Super" | "PrivateIdentifier" | "TemplateElement"
  | "EmptyStatement" | "DebuggerStatement" | "BreakStatement" | "ContinueStatement" => pure ()
  | "ArrayExpression" => for e in items (fld j "elements") do visit bound inFn e
  | "ObjectExpression" => for p in items (fld j "properties") do visit bound inFn p
  | "Property" =>
    if flag j "computed" then visit bound inFn (fld j "key")
    visit bound inFn (fld j "value")
  | "SpreadElement" | "AwaitExpression" | "YieldExpression" | "UnaryExpression" | "ReturnStatement"
  | "ThrowStatement" => visit bound inFn (fld j "argument")
  | "ChainExpression" | "ParenthesizedExpression" | "ExpressionStatement" =>
    visit bound inFn (fld j "expression")
  | "BinaryExpression" | "LogicalExpression" =>
    visit bound inFn (fld j "left"); visit bound inFn (fld j "right")
  | "ConditionalExpression" | "IfStatement" =>
    visit bound inFn (fld j "test"); visit bound inFn (fld j "consequent")
    visit bound inFn (fld j "alternate")
  | "AssignmentExpression" => target bound inFn (fld j "left"); visit bound inFn (fld j "right")
  | "UpdateExpression" => target bound inFn (fld j "argument")
  | "CallExpression" | "NewExpression" =>
    visit bound inFn (fld j "callee")
    for a in items (fld j "arguments") do visit bound inFn a
  | "MemberExpression" =>
    visit bound inFn (fld j "object")
    if flag j "computed" then visit bound inFn (fld j "property")
  | "SequenceExpression" | "TemplateLiteral" => for e in items (fld j "expressions") do visit bound inFn e
  | "TaggedTemplateExpression" => visit bound inFn (fld j "tag"); visit bound inFn (fld j "quasi")
  | "ImportExpression" => visit bound inFn (fld j "source"); visit bound inFn (fld j "options")
  | "FunctionDeclaration" | "FunctionExpression" | "ArrowFunctionExpression" => visitFn bound j
  | "ClassDeclaration" | "ClassExpression" => visitClass bound inFn j
  | "VariableDeclaration" =>
    for d in items (fld j "declarations") do
      visitPat bound inFn (fld d "id")
      visit bound inFn (fld d "init")
  | "BlockStatement" | "StaticBlock" =>
    let stmts := items (fld j "body")
    let b := lexicalNames stmts ++ bound
    for s in stmts do visit b inFn s
  | "ForStatement" =>
    let b := lexicalNames #[fld j "init"] ++ bound
    visit b inFn (fld j "init"); visit b inFn (fld j "test")
    visit b inFn (fld j "update"); visit b inFn (fld j "body")
  | "ForInStatement" | "ForOfStatement" =>
    let left := fld j "left"
    let b := lexicalNames #[left] ++ bound
    if typ left == "VariableDeclaration" then visit b inFn left else target b inFn left
    visit b inFn (fld j "right"); visit b inFn (fld j "body")
  | "WhileStatement" | "DoWhileStatement" =>
    visit bound inFn (fld j "test"); visit bound inFn (fld j "body")
  | "LabeledStatement" => visit bound inFn (fld j "body")
  | "TryStatement" =>
    visit bound inFn (fld j "block")
    let h := fld j "handler"
    unless isNull h do
      let b := binders (fld h "param") ++ bound
      visitPat b inFn (fld h "param")
      visit b inFn (fld h "body")
    visit bound inFn (fld j "finalizer")
  | "SwitchStatement" =>
    visit bound inFn (fld j "discriminant")
    let cases := items (fld j "cases")
    let b := lexicalNames (cases.flatMap fun c => items (fld c "consequent")) ++ bound
    for c in cases do
      visit b inFn (fld c "test")
      for s in items (fld c "consequent") do visit b inFn s
  | "ExportNamedDeclaration" =>
    visit bound inFn (fld j "declaration")
    if isNull (fld j "source") then
      for s in items (fld j "specifiers") do visit bound inFn (fld s "local")
  | "ExportDefaultDeclaration" => visit bound inFn (fld j "declaration")
  | "ImportDeclaration" | "ExportAllDeclaration" => pure ()
  | t => noteFallback t; for c in children j do visit bound inFn c

/-- Expression parts of a binding pattern: defaults and computed keys. -/
partial def visitPat (bound : List String) (inFn : Bool) (p : Json) : M Unit := do
  match typ p with
  | "" | "Identifier" => pure ()
  | "ObjectPattern" =>
    for q in items (fld p "properties") do
      if typ q == "RestElement" then visitPat bound inFn (fld q "argument")
      else
        if flag q "computed" then visit bound inFn (fld q "key")
        visitPat bound inFn (fld q "value")
  | "ArrayPattern" => for e in items (fld p "elements") do visitPat bound inFn e
  | "AssignmentPattern" => visitPat bound inFn (fld p "left"); visit bound inFn (fld p "right")
  | "RestElement" => visitPat bound inFn (fld p "argument")
  | _ => visit bound inFn p

/-- Assignment target: a write to a non-local name is recorded. -/
partial def target (bound : List String) (inFn : Bool) (t : Json) : M Unit := do
  match typ t with
  | "Identifier" =>
    let n := str (fld t "name")
    unless bound.contains n do
      modify fun s => if inFn then { s with assignedLazy := s.assignedLazy.push n } else { s with assignedEager := s.assignedEager.push n }
    ref bound inFn n
  | "ObjectPattern" =>
    for q in items (fld t "properties") do
      if typ q == "RestElement" then target bound inFn (fld q "argument")
      else
        if flag q "computed" then visit bound inFn (fld q "key")
        target bound inFn (fld q "value")
  | "ArrayPattern" => for e in items (fld t "elements") do target bound inFn e
  | "AssignmentPattern" => target bound inFn (fld t "left"); visit bound inFn (fld t "right")
  | "RestElement" => target bound inFn (fld t "argument")
  | _ => visit bound inFn t

/-- Function bodies run when called, so everything inside is lazy. Parameter defaults cannot see
the body's own declarations. -/
partial def visitFn (bound : List String) (j : Json) : M Unit := do
  let params := items (fld j "params")
  let self := if typ j == "FunctionExpression" then binders (fld j "id") else []
  let outer := (params.toList.flatMap binders) ++ self ++ bound
  for p in params do visitPat outer true p
  let body := fld j "body"
  if typ body == "BlockStatement" then
    let stmts := items (fld body "body")
    let inner := (stmts.toList.flatMap varNames) ++ lexicalNames stmts ++ outer
    for s in stmts do visit inner true s
  else visit outer true body

/-- `extends`, computed keys and static initializers run at definition; methods and instance
field initializers run later. -/
partial def visitClass (bound : List String) (inFn : Bool) (j : Json) : M Unit := do
  visit bound inFn (fld j "superClass")
  let inner := binders (fld j "id") ++ bound
  for m in items (fld (fld j "body") "body") do
    match typ m with
    | "MethodDefinition" =>
      if flag m "computed" then visit inner inFn (fld m "key")
      visitFn inner (fld m "value")
    | "PropertyDefinition" =>
      if flag m "computed" then visit inner inFn (fld m "key")
      visit inner (if flag m "static" then inFn else true) (fld m "value")
    | "StaticBlock" => visit inner inFn m
    | t => noteFallback t; for c in children m do visit inner inFn c

end

/-! ## Inert statements: the allow-list -/

def classInert (j : Json) : Bool :=
  isNull (fld j "superClass") && (items (fld (fld j "body") "body")).all fun m =>
    match typ m with
    | "MethodDefinition" => !flag m "computed"
    | "PropertyDefinition" => !flag m "computed" && (!flag m "static" || isNull (fld m "value"))
    | _ => false

partial def inertExpr (j : Json) : Bool :=
  match typ j with
  | "Identifier" | "Literal" | "FunctionExpression" | "ArrowFunctionExpression" => true
  | "TemplateLiteral" => (items (fld j "expressions")).isEmpty
  | "ArrayExpression" =>
    (items (fld j "elements")).all fun e => isNull e || (typ e != "SpreadElement" && inertExpr e)
  | "ObjectExpression" =>
    (items (fld j "properties")).all fun p =>
      typ p == "Property" && !flag p "computed" && inertExpr (fld p "value")
  | "ClassExpression" => classInert j
  | "UnaryExpression" =>
    let op := str (fld j "operator")
    ((op == "void" || op == "typeof") && inertExpr (fld j "argument"))
      || (["-", "+", "!", "~"].contains op && typ (fld j "argument") == "Literal")
  | _ => false

partial def inertTop (s : Json) : Bool :=
  match typ s with
  | "FunctionDeclaration" | "EmptyStatement" => true
  | "ClassDeclaration" => classInert s
  | "VariableDeclaration" =>
    (items (fld s "declarations")).all fun d =>
      typ (fld d "id") == "Identifier" && (isNull (fld d "init") || inertExpr (fld d "init"))
  | "ExpressionStatement" => inertExpr (fld s "expression")
  | "ExportNamedDeclaration" => isNull (fld s "declaration") || inertTop (fld s "declaration")
  | "ExportDefaultDeclaration" =>
    let d := fld s "declaration"
    typ d == "FunctionDeclaration" || (typ d == "ClassDeclaration" && classInert d) || inertExpr d
  | _ => false

/-- A single lexical constant initialized directly with a primitive literal.
Regex and BigInt literals are excluded; the source AST is trusted. -/
def primitiveConstant (s : Json) : Bool :=
  let ds := items (fld s "declarations")
  let d := ds[0]?.getD Json.null
  let init := fld d "init"
  typ s == "VariableDeclaration" && str (fld s "kind") == "const" && ds.size == 1 &&
    typ (fld d "id") == "Identifier" && typ init == "Literal" &&
    isNull (fld init "regex") && isNull (fld init "bigint")

/-! ## Top-level statements -/

partial def topDefs (s : Json) : List String :=
  match typ s with
  | "FunctionDeclaration" | "ClassDeclaration" => binders (fld s "id")
  | "VariableDeclaration" => (items (fld s "declarations")).toList.flatMap fun d => binders (fld d "id")
  | "ImportDeclaration" => (items (fld s "specifiers")).toList.map fun sp => str (fld (fld sp "local") "name")
  | "ExportNamedDeclaration" => if isNull (fld s "declaration") then [] else topDefs (fld s "declaration")
  | "ExportDefaultDeclaration" =>
    let d := fld s "declaration"
    if typ d == "FunctionDeclaration" || typ d == "ClassDeclaration" then binders (fld d "id") else []
  -- a `var` inside a block, loop or `try` at the top level still declares a module-scope binding
  | _ => varNames s

def isHoisted (s : Json) : Bool :=
  typ s == "FunctionDeclaration"
    || ((typ s == "ExportNamedDeclaration" || typ s == "ExportDefaultDeclaration")
        && typ (fld s "declaration") == "FunctionDeclaration")

/-- Count only candidate spellings and `eval`, including shadowed occurrences and
deferred bodies. Counts saturate at three: only exactly two or no occurrences matter. -/
abbrev IdentityNames := Std.TreeMap.Raw String Unit
abbrev IdentityCounts := Std.TreeMap.Raw String Nat

def identityCount (counts : IdentityCounts) (name : String) : Nat :=
  counts.getD name 0

def countIdentifiers (fuel : Nat) (wanted : IdentityNames) (j : Json)
    (counts : IdentityCounts) : Option IdentityCounts := do
  match fuel with
  | 0 => none
  | fuel + 1 =>
    let counts := if typ j == "Identifier" then
        let n := str (fld j "name")
        if n == "eval" || wanted.contains n then
          counts.insert n (min 3 (identityCount counts n + 1))
        else counts
      else counts
    (children j).foldlM (fun counts c => countIdentifiers fuel wanted c counts) counts

/-- Depth budget for the complete annotated tree. Exhaustion refuses every
identity inference; it never treats a partially scanned tree as safe. -/
def identityScanFuel : Nat := 256

def scanIdentifiers (fuel : Nat) (wanted : IdentityNames) (sources : Array Json) :
    Option IdentityCounts :=
  sources.foldlM (fun counts source => countIdentifiers fuel wanted source counts) {}

/-- Factory inference needs exact counts when a factory has multiple direct calls. -/
partial def countFactoryIdentifiers (wanted : Std.HashSet String) (j : Json)
    (counts : Std.HashMap String Nat) : Std.HashMap String Nat := Id.run do
  let mut counts := counts
  if typ j == "Identifier" then
    let n := str (fld j "name")
    if n == "eval" || wanted.contains n then
      counts := counts.insert n (counts.getD n 0 + 1)
  for c in children j do counts := countFactoryIdentifiers wanted c counts
  return counts

/-- A module-scoped, hoisted function whose whole body returns its only parameter.
The frontend's binder IDs must agree; raw, unannotated ESTree does not qualify. -/
def identityDeclaration (s : Json) : Option String := do
  guard (typ s == "FunctionDeclaration" && !flag s "async" && !flag s "generator")
  let id := fld s "id"
  guard (typ id == "Identifier" && str (fld id "k") == "T" && !isNull (fld id "d"))
  let params := items (fld s "params")
  guard (params.size == 1)
  let p := params[0]!
  let body := fld s "body"
  let stmts := items (fld body "body")
  guard (typ p == "Identifier" && str (fld p "k") == "L" && !isNull (fld p "b"))
  guard (typ body == "BlockStatement" && stmts.size == 1 && typ stmts[0]! == "ReturnStatement")
  let result := fld stmts[0]! "argument"
  let pb ← (fld p "b").getNat?.toOption
  let rb ← (fld result "b").getNat?.toOption
  guard (typ result == "Identifier" && str (fld result "k") == "L" && pb == rb)
  some (str (fld id "name"))

/-- Only one plain direct call with a single-digit numeric literal is supported. -/
def identityCall (s : Json) : Option String := do
  guard (typ s == "VariableDeclaration" && str (fld s "kind") == "const")
  let ds := items (fld s "declarations")
  guard (ds.size == 1 && typ (fld ds[0]! "id") == "Identifier")
  let call := fld ds[0]! "init"
  let callee := fld call "callee"
  let args := items (fld call "arguments")
  guard (typ call == "CallExpression" && !flag call "optional" && typ callee == "Identifier")
  guard (str (fld callee "k") == "T" && args.size == 1 && typ args[0]! == "Literal")
  guard ((["n:0", "n:1", "n:2", "n:3", "n:4", "n:5", "n:6", "n:7", "n:8", "n:9"] : List String).contains (str (fld args[0]! "lit")))
  some (str (fld callee "name"))

/-- Ordinary function whose sole statement creates a fresh deferred arrow. -/
def factoryDeclaration (s : Json) : Option String := do
  guard (typ s == "FunctionDeclaration" && !flag s "async" && !flag s "generator")
  let id := fld s "id"
  let params := items (fld s "params")
  guard (typ id == "Identifier" && str (fld id "k") == "T" && !isNull (fld id "d"))
  guard (params.size == 1 && typ params[0]! == "Identifier" && str (fld params[0]! "k") == "L" && !isNull (fld params[0]! "b"))
  let body := fld s "body"
  let ss := items (fld body "body")
  guard (typ body == "BlockStatement" && ss.size == 1 && typ ss[0]! == "ReturnStatement")
  guard (typ (fld ss[0]! "argument") == "ArrowFunctionExpression")
  some (str (fld id "name"))

/-- A direct call with exactly one module binding as argument. -/
def factoryCall (s : Json) : Option (String × String) := do
  guard (typ s == "VariableDeclaration" && str (fld s "kind") == "const")
  let ds := items (fld s "declarations")
  guard (ds.size == 1 && typ (fld ds[0]! "id") == "Identifier")
  let call := fld ds[0]! "init"
  let callee := fld call "callee"
  let args := items (fld call "arguments")
  guard (typ call == "CallExpression" && !flag call "optional" && typ callee == "Identifier" && str (fld callee "k") == "T")
  guard (args.size == 1 && typ args[0]! == "Identifier" && str (fld args[0]! "k") == "T")
  some (str (fld callee "name"), str (fld args[0]! "name"))

structure Top where
  defs     : List String
  rawEager : Array String
  rawLazy  : Array String
  assignedEager : Array String
  assignedLazy  : Array String
  fallback : Array String
  hoisted  : Bool
  inert    : Bool
  factoryDef : Option String
  factoryCall : Option (String × String)
  identityDef : Option String
  identityCall : Option String
  source : Json

def analyzeTop (s : Json) : Top :=
  let acc := (visit [] false s).run {} |>.2
  { defs := topDefs s, rawEager := acc.eager, rawLazy := acc.lazy, assignedEager := acc.assignedEager, assignedLazy := acc.assignedLazy,
    fallback := acc.fallback, hoisted := isHoisted s, inert := inertTop s,
    factoryDef := factoryDeclaration s, factoryCall := factoryCall s,
    identityDef := identityDeclaration s, identityCall := identityCall s,
    source := s }

structure Footprint where
  defs     : List String
  eager    : List String
  lazy     : List String
  hoisted  : Bool
  effect   : Bool
  observedInit : Bool
  /-- names the statement may assign when it runs, including through the functions it may call -/
  writes   : List String
  /-- names that the code of the statement assigns, while it runs or in function bodies it creates -/
  assigns  : List String
  fallback : List String
  rawEager : List String
  rawLazy  : List String
  /-- A source-derived trusted identity-call inference, if used for this unit. -/
  inferredIdentity : Option String := none
  inferredFactory : Option String := none

def dedupe (a : Array String) : List String := a.toList.eraseDups

/-- Names reachable from `start` through what the declarations of each name mention, and every
name the function bodies created by those declarations assign. -/
partial def reachWrites (mentions lazyW : Std.HashMap String (Array String)) :
    List String → Std.HashSet String → Std.HashSet String → Std.HashSet String
  | [], _, out => out
  | u :: work, seen, out =>
    if seen.contains u then reachWrites mentions lazyW work seen out
    else
      reachWrites mentions lazyW ((mentions.getD u #[]).toList ++ work) (seen.insert u)
        ((lazyW.getD u #[]).foldl (·.insert ·) out)

/-- Determine stable identity names from the exact annotated trees used by the
frontend. `none` means the scan did not finish, never a partial result. -/
def inferredIdentities (fuel : Nat) (tops : Array Top) : Option IdentityNames := do
  let definitions : IdentityNames := tops.foldl (fun seen t =>
    match t.identityDef with
    | some n => seen.insert n ()
    | none => seen) {}
  let candidates : IdentityNames := tops.foldl (fun seen t =>
    match t.identityCall with
    | some n => if definitions.contains n then seen.insert n () else seen
    | none => seen) {}
  let identifiers ← if candidates.isEmpty then some ({} : IdentityCounts) else
    scanIdentifiers fuel candidates (tops.map (·.source))
  if identityCount identifiers "eval" != 0 then return {}
  let identities : IdentityNames := tops.foldl (fun seen t =>
    match t.identityDef with
    | some n => if identityCount identifiers n == 2 then seen.insert n () else seen
    | none => seen) {}
  return identities

/-- The footprint the checker needs. Two rules make a statement an effect: it is not in the allow-list of
inert shapes, or it reads an undeclared name. A statement with effects counts everything it mentions as eager
(a call may run any function it can reach), and the names it may assign are those assigned by the statement
itself and by every function body it can reach. -/
def finalizeWithScanStatus (tops : Array Top) (externallyVisible : Array String := #[]) : Array Footprint × Bool :=
  let scan := inferredIdentities identityScanFuel tops
  let identities := scan.getD {}
  let factories := tops.foldl (fun seen t => match t.factoryDef with
    | some n => seen.insert n
    | none => seen) ({} : Std.HashSet String)
  let candidates := tops.foldl (fun seen t =>
    match t.factoryCall with
    | some (n, _) => if factories.contains n then seen.insert n else seen
    | none => seen) ({} : Std.HashSet String)
  let factoryIdentifiers := if candidates.isEmpty then ({} : Std.HashMap String Nat) else
    tops.foldl (fun counts t => countFactoryIdentifiers candidates t.source counts) ({} : Std.HashMap String Nat)
  let factoryCalls : Std.HashMap String Nat := tops.foldl (fun counts t =>
    match t.factoryCall with
    | some (n, _) => counts.insert n (counts.getD n 0 + 1)
    | none => counts) {}
  let defCounts : Std.HashMap String Nat := tops.foldl (fun counts t =>
    t.defs.foldl (fun counts n => counts.insert n (counts.getD n 0 + 1)) counts) {}
  let assigned : Std.HashSet String := tops.foldl (fun seen t =>
    (t.assignedEager ++ t.assignedLazy).foldl (fun seen n => seen.insert n) seen) {}
  let stableArgs : Std.HashSet String := tops.foldl (fun seen t =>
    if t.hoisted then t.defs.foldl (fun seen n =>
      if defCounts.getD n 0 == 1 && !assigned.contains n then seen.insert n else seen) seen
    else seen) {}
  let declared : Std.HashSet String := tops.foldl (fun s t => t.defs.foldl (·.insert ·) s) {}
  let mentions : Std.HashMap String (Array String) := tops.foldl (fun m t =>
    t.defs.foldl (fun m d => m.insert d (m.getD d #[] ++ t.rawEager ++ t.rawLazy)) m) {}
  let lazyW : Std.HashMap String (Array String) := tops.foldl (fun m t =>
    t.defs.foldl (fun m d => m.insert d (m.getD d #[] ++ t.assignedLazy.filter declared.contains)) m) {}
  -- A result captured by deferred code may be read by an earlier-escaped callback.
  -- An inert initializer that reads it may also create an alias for such a callback.
  -- Unknown syntax may hide deferred behavior; treat the whole statement conservatively.
  let indirect : Std.HashSet String := tops.foldl (fun seen t =>
    let refs := t.rawLazy ++ (if t.inert || !t.fallback.isEmpty then t.rawEager else #[])
    refs.foldl (fun seen n => seen.insert n) seen)
      (externallyVisible.foldl (fun seen n => seen.insert n) {})
  let footprints := tops.map fun t =>
    let unobserved := t.defs.all (!indirect.contains ·)
    let identityInert := t.identityCall.any identities.contains && unobserved
    let factoryInert := unobserved && !factoryIdentifiers.contains "eval" &&
      t.factoryCall.any (fun (n, arg) => factories.contains n && stableArgs.contains arg && factoryIdentifiers.getD n 0 == factoryCalls.getD n 0 + 1)
    -- An inert, non-hoisted initializer can still be observed by a callback
    -- that escaped earlier and runs indirectly before this binding initializes.
    let observedInert := t.inert && !t.hoisted && !unobserved
    let observedInit := observedInert && primitiveConstant t.source
    let effect := !(t.inert || identityInert || factoryInert) || (observedInert && !observedInit) || t.rawEager.any (!declared.contains ·)
    let eager := if effect then dedupe (t.rawEager ++ t.rawLazy) else dedupe t.rawEager
    let own := dedupe (t.assignedEager.filter declared.contains)
    let writes := if effect then (own ++ (reachWrites mentions lazyW eager {} {}).toList).eraseDups else own
    { defs := t.defs, eager, lazy := dedupe t.rawLazy, hoisted := t.hoisted, effect, observedInit, writes,
      assigns := dedupe ((t.assignedEager ++ t.assignedLazy).filter declared.contains),
      fallback := dedupe t.fallback,
      rawEager := dedupe t.rawEager, rawLazy := dedupe t.rawLazy,
      inferredIdentity := if identityInert && !effect then t.identityCall else none,
      inferredFactory := if factoryInert && !effect then t.factoryCall.map Prod.fst else none }
  (footprints, scan.isNone)

def finalize (tops : Array Top) : Array Footprint := (finalizeWithScanStatus tops).1

def Footprint.toJson (f : Footprint) : Json :=
  Json.mkObj [("defs", Lean.toJson f.defs), ("eager", Lean.toJson f.eager), ("lazy", Lean.toJson f.lazy),
    ("hoisted", Lean.toJson f.hoisted), ("effect", Lean.toJson f.effect), ("observedInit", Lean.toJson f.observedInit),
    ("writes", Lean.toJson f.writes), ("assigns", Lean.toJson f.assigns), ("fallback", Lean.toJson f.fallback),
    ("rawEager", Lean.toJson f.rawEager), ("rawLazy", Lean.toJson f.rawLazy)]

end Lower
