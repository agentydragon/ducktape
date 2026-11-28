import EsmSplit.CoreJS.LinkedPivotCheck
import EsmSplit.CoreJS.LinkWorld
import EsmSplit.CoreJS.ModuleLocation
import EsmSplit.CoreJS.StateMatch
import EsmSplit.CoreJS.StaticStarExports
import EsmSplit.ESTreeUnits
import EsmSplit.Plan

/-! Check the exact disk readback of moving a leading block of named requests
with one private const into an adjacent worker. The default snapshot stays in
the facade. The address-only check does not admit an executable deployment. -/
namespace CoreJS.StaticImportMotion

open Lean (Json)
open EsmSplit (Spec Selector)
open LinkedBehavior

private def file (files : Json) (path : String) : Except String (String × Array Json) := do
  let ast ← files.getObjVal? path
  let text ← ast.getObjValAs? String "sourceText"
  return (text, ← ESTreeUnits.checkedNodes ast text)

private def ordinals (selectors : List Selector) : Except String (List Nat) :=
  selectors.mapM fun
    | .stmt id => pure id
    | .name _ => throw "import motion requires exact source ordinals"

-- AST offsets and the printer both use UTF-16 code units.
private def charOffset (source : String) (offset : Nat) : Nat := Id.run do
  let mut units := 0
  let mut chars := 0
  for ch in source do
    if units == offset then break
    units := units + (if ch.toNat > 0xffff then 2 else 1)
    chars := chars + 1
  return chars

private def imports (path : String) (rows : Array Json) : Except String (List Import) := do
  rows.toList.mapM fun row => do
    let specs ← row.getObjValAs? (Array Json) "specifiers"
    unless !specs.isEmpty && specs.all (fun sp => Lower.typ sp == "ImportSpecifier") do
      throw "moved requests must be named imports"
    ModuleInput.importDecl path row

private def binding (row : Json) : Except String Name := do
  unless Lower.typ row == "VariableDeclaration" &&
      (row.getObjValAs? String "kind").toOption == some "const" do
    throw "expected one private const immediately after the imports"
  let #[decl] ← row.getObjValAs? (Array Json) "declarations" | throw "expected one declarator"
  decl.getObjVal? "id" >>= FromESTree.identifier

private partial def suspendsModule (node : Json) : Bool :=
  let kind := Lower.typ node
  if ["FunctionDeclaration", "FunctionExpression", "ArrowFunctionExpression"].contains kind then false
  else kind == "AwaitExpression" ||
    (kind == "ForOfStatement" && Lower.flag node "await") ||
    (kind == "VariableDeclaration" && (node.getObjValAs? String "kind").toOption == some "await using") ||
    (Lower.children node).any suspendsModule

private partial def mentionsEval (node : Json) : Bool :=
  (Lower.typ node == "Identifier" && (node.getObjValAs? String "name").toOption == some "eval") ||
    (Lower.children node).any mentionsEval

private def cell (links : StaticStarExports.NamespaceLinks) (path name : String) : Except String Addr := do
  let some a := (← links.exports path).lookup name | throw s!"{path}: missing export {name}"
  return a

structure Readback where
  private mk ::
  before : StaticStarExports.Graph
  after : StaticStarExports.Graph
  private offsets : List (String × Nat)
  entry : String
  facade : String
  worker : String
  importCount : Nat
  requestCount : Nat
  order : List String
  orderAfter : List String

/-- Input ASTs must come from the existing parser, including the independently
reread staged files. Exact byte comparisons limit the only edits to the two
glue declarations and the authored move. -/
def check (entry : String) (spec : Spec) (original reread : Json) : Except String Readback := do
  let facade := spec.entry
  let [worker] := spec.order | throw "expected one authored worker"
  unless spec.sequential && !spec.privateEntryBody &&
      decide ((spec.modules.map Prod.fst).Perm [facade, worker]) do
    throw "expected fixed facade and one sequential worker"
  let filename := (worker.splitOn "/").getLastD ""
  let request := "./" ++ filename
  unless worker != facade && (← ModuleInput.resolve facade request) == worker do
    throw "worker must be a new adjacent module"
  let paths := (← original.getObj?).toList.map Prod.fst
  unless !paths.contains worker && decide (((← reread.getObj?).toList.map Prod.fst).Perm (worker :: paths)) do
    throw "reread files differ from the declared module set"
  let (text, rows) ← file original facade
  unless rows.size ≥ 3 do throw "expected named imports, a const and a default snapshot"
  let count := rows.size - 2
  let declaration := rows[count]!
  let snapshot := rows[count + 1]!
  let name ← binding declaration
  if mentionsEval declaration then throw "moved declaration refers to eval"
  unless Lower.typ snapshot == "ExportDefaultDeclaration" &&
      (← snapshot.getObjVal? "declaration" >>= FromESTree.identifier) == name do
    throw "facade must retain the original default expression"
  unless (← ordinals ((spec.modules.lookup worker).getD [])) == List.range (count + 1) &&
      (← ordinals ((spec.modules.lookup facade).getD [])) == [count + 1] do
    throw "authored rows differ from the named import extraction"
  let requests ← imports facade (rows.extract 0 count)
  let mut offsets := []
  let mut next := 0
  for path in paths do
    let (sourceText, nodes) ← file original path
    let ast ← original.getObjVal? path
    if nodes.any suspendsModule then throw s!"{path}: asynchronous module evaluation is unsupported"
    unless (← ModuleLocation.uses path ast).isEmpty do
      throw s!"{path}: only location-independent static input is supported"
    offsets := offsets ++ [(path, next)]
    next := next + nodes.size
    if path != facade then
      unless (← file reread path).1 == sourceText do throw s!"{path}: fixed provider bytes changed"
  let before ← StaticStarExports.resolve original
  let after ← StaticStarExports.resolve reread
  let order ← before.order entry
  let orderAfter ← after.order entry
  unless orderAfter == order.flatMap (fun path => if path == facade then [worker, facade] else [path]) do
    throw "module evaluation order or instance changed"
  let (workerText, workerRows) ← file reread worker
  unless (← imports worker (workerRows.extract 0 count)) == requests do
    throw "moved requests differ in provider, request order or named bindings"
  let (facadeText, facadeRows) ← file reread facade
  unless facadeRows.size == 2 && workerRows.size == count + 1 &&
      (← ModuleInput.importDecl facade facadeRows[0]!) == ⟨worker, [(name, name)]⟩ do
    throw "unexpected facade or worker glue"
  let defStart := charOffset text (← declaration.getObjValAs? Nat "start")
  let snapshotStart := charOffset text (← snapshot.getObjValAs? Nat "start")
  let importText := (text.take defStart).toString
  let moved := ((text.drop defStart).take (snapshotStart - defStart)).toString
  let retained := (text.drop snapshotStart).toString
  unless workerText == importText ++ "export " ++ moved &&
      facadeText == s!"import \{ {name} } from {(Json.str request).compress};\n" ++ retained do
    throw "reread differs from the original rows and exact glue"
  for path in paths do
    unless decide ((← before.origins path).Perm (← after.origins path)) do
      throw s!"{path}: public export origins changed"
  let sourceLinks ← before.linkNamespaceCells entry
  let outputLinks ← after.linkNamespaceCells entry
  for req in requests do
    for (publicName, localName) in req.names do
      unless (← sourceLinks.localCell facade localName) == (← cell sourceLinks req.source publicName) &&
          (← outputLinks.localCell worker localName) == (← cell outputLinks req.source publicName) do
        throw s!"{localName}: import is not linked to the original provider binding"
  let checkSnapshot (links : StaticStarExports.NamespaceLinks) (owner : String) : Except String Unit := do
    let provider ← links.localCell owner name
    unless (← links.localCell facade name) == provider &&
        (← cell links facade "default") != provider do
      throw "default snapshot must have its own cell and read the original binding"
  checkSnapshot sourceLinks facade
  checkSnapshot outputLinks worker
  return ⟨before, after, offsets, entry, facade, worker, count, requests.length, order, orderAfter⟩

structure CheckedPrograms where
  before : Program
  after : Program
  exports : SameExports before after
  equal : ∀ w fuel, runProgram w fuel before = runProgram w fuel after

/-- Where concrete lowering is possible, check all linked executable units and
captures in order with their original identities. Transport equality from the
link state to every world and fuel. This is separate from the address check. -/
def Readback.checkPrograms (checked : Readback) : Except String CheckedPrograms := do
  let offset path := (checked.offsets.lookup path).getD 0
  let before ← checked.before.lower checked.entry (some fun path id => offset path + id)
  let after ← checked.after.lower checked.entry (some fun path id =>
    if path == checked.worker then offset checked.facade + id
    else if path == checked.facade then offset path + checked.importCount + 1
    else offset path + id)
  let links ← checkProbePair before after
  for (path, _) in checked.offsets do
    unless decide ((← EntryExports.resolve path links.before.linked).Perm
        (← EntryExports.resolve path links.after.linked)) do
      throw s!"{path}: linked public cells differ"
  let .isTrue exports := (inferInstance : Decidable (exportsMatchFrom links.before links.after = true))
    | throw "linked root exports differ"
  let some ⟨ready⟩ := StateMatch.probe links.before.ready links.after.ready
    | throw "ready states differ"
  let some ⟨units⟩ := sameUnits? (unitsOf links.before.linked) (unitsOf links.after.linked)
    | throw "ordered initialized units or captures differ"
  return ⟨before, after, links.sameExports exports, fun w fuel => by
    rw [run_after_link fuel (links.before.forWorld w), run_after_link fuel (links.after.forWorld w)]
    change runUnits fuel (unitsOf links.before.linked) (linkState w links.before.ready) =
      runUnits fuel (unitsOf links.after.linked) (linkState w links.after.ready)
    rw [ready, runUnits_same fuel units]⟩

end CoreJS.StaticImportMotion
