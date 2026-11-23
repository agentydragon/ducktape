import EsmSplit.CoreJS.ModuleLocation

/-! Locate the live module bindings a dynamic import can obtain through its static dependency graph.
The caller supplies the module names on the CoreJS side of the boundary and parsed JavaScript files
outside it. For each edge into CoreJS, the deployment must supply the actual module record and live
exports the loader returns. Empty imports still require the module's initialization. -/

namespace CoreJS.DynamicImports

open Lean (Json)
open ModuleLocation (Resource)

structure LinkedRequest where
  importer : String
  source : String
  exports : List Name
  deriving Repr, BEq

structure Boundary where
  /-- External scripts whose effective URLs and sources match in both deployments. -/
  scripts : List String := []
  /-- Each static import of a linked module from those scripts, including side-effect-only imports. -/
  requests : List LinkedRequest := []
  deriving Repr

private def requests (name : String) (ast : Json) (resource : Resource) : Except String (Array Import) := do
  unless (← ast.getObjValAs? String "sourceText") == resource.source do throw "parsed source differs from resource"
  let mut imports := #[]
  for node in ← ESTreeUnits.checkedNodes ast resource.source do
    if let some why := Lower.outsideModel false node then throw why
    for use in ← ModuleLocation.uses name node do
      if let .dynamicImport .. := use then throw "nested dynamic import"
    match Lower.typ node with
    | "ImportDeclaration" => imports := imports.push (← ModuleInput.importDecl name node)
    | "ExportAllDeclaration" => throw "unsupported re-export from another module"
    | "ExportNamedDeclaration" =>
      unless (← node.getObjVal? "source") == .null do throw "unsupported re-export from another module"
    | _ => pure ()
  return imports

/-- Traverse one literal relative import target from the location check. Stop at a linked module,
recording every name imported from it. For a certificate the linked module names must come from the
checked module graph. Resolving the recorded names to corresponding live cells, module-cache and
promise behavior remain deployment correspondence obligations. -/
def check (target : String) (linked : List String) (parsed : Json)
    (before after : String → Option Resource) : Except String Boundary := do
  EsmSplit.validateModulePaths (target :: linked)
  if linked.contains target then throw s!"dynamic import directly requests a linked namespace: {target}"
  let files ← parsed.getObj?
  let rec visit (fuel : Nat) (name : String) (b : Boundary) : Except String Boundary := do
    if b.scripts.contains name then return b
    match fuel with
    | 0 => throw "static dependency traversal exhausted its input files"
    | k + 1 =>
      let some ast := files.get? name | throw s!"missing parsed dynamic dependency {name}"
      let some resource := before name | throw s!"missing original dynamic dependency {name}"
      unless !resource.effectiveUrl.isEmpty && after name == some resource do
        throw s!"dynamic dependency URL or source changed: {name}"
      let imports ← (requests name ast resource).mapError fun why => s!"{name}: {why}"
      let mut b := { b with scripts := name :: b.scripts }
      for req in imports do
        if linked.contains req.source then
          let edge : LinkedRequest := ⟨name, req.source, req.names.map Prod.fst⟩
          b := { b with requests := edge :: b.requests }
        else b ← visit k req.source b
      return b
  let b ← visit (files.size + 1) target {}
  return ⟨b.scripts.reverse, b.requests.reverse⟩

end CoreJS.DynamicImports
