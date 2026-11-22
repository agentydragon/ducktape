import Experiments.ESTreeUnits
import Experiments.Lower
import Std.Data.HashMap

open Lean

namespace TranslateInput

open Lower (fld typ)

/-- A module's ordered imports, including imports for side effects. -/
structure Import where
  source : String
  specs : Array (String × String)
  deriving Inhabited

structure Module where
  units : Array Json := #[]
  imports : Array Import := #[]
  exports : Array (String × String) := #[]
  deriving Inhabited

structure Bundle where
  units : Array Json
  imports : Std.HashMap String (String × String)
  exports : Array (String × String)

/-- Canonicalize nodes for structural comparison, keeping binder identities and literal meanings. -/
partial def compact (j : Json) : Json :=
  match j with
  | .arr xs => .arr (xs.map compact)
  | .obj fields =>
    match typ j with
    | "Identifier" =>
      if fld j "k" == .null then Json.mkObj [("type", "Name"), ("name", fld j "name")]
      else j
    | "Literal" => Json.mkObj [("type", "Literal"), ("lit", fld j "lit")]
    | "TemplateElement" =>
      let value := fld j "value"
      Json.mkObj [("type", "TemplateElement"), ("cooked", fld value "cooked"), ("raw", fld value "raw"), ("tail", fld j "tail")]
    | t =>
      Json.mkObj (fields.foldl (fun acc k v =>
        if ["start", "end", "range", "loc", "raw", "sourceType", "sourceText"].contains k then acc
        else (k, if t == "Property" && k == "shorthand" then .bool false else compact v) :: acc) [])
  | _ => j

private def specName (j : Json) : Except String String := do
  match typ j with
  | "Identifier" => (fld j "name").getStr?
  | "Literal" =>
    let lit ← (fld j "lit").getStr?
    unless lit.startsWith "s:" do throw "expected a string literal in import or export"
    return (lit.drop 2).toString
  | _ => throw "expected a name in import or export"

private def importSpec (j : Json) : Except String (String × String) := do
  let localName ← specName (fld j "local")
  let imported ← match typ j with
    | "ImportDefaultSpecifier" => pure "default"
    | "ImportNamespaceSpecifier" => pure "*"
    | "ImportSpecifier" => specName (fld j "imported")
    | _ => throw "unsupported import specifier"
  return (localName, imported)

/-- Translation certificates number executable body units. Import declarations and export lists supply
linkage, while inline named exports also contribute a body unit for each declaration. -/
def readModule (input : Json) : Except String Module := do
  let nodes ← ESTreeUnits.nodes (fld input "program")
  let mut result : Module := {}
  for node in nodes do
    match typ node with
    | "ImportDeclaration" =>
      let source ← specName (fld node "source")
      let specs ← (← (fld node "specifiers").getArr?).mapM importSpec
      result := { result with imports := result.imports.push { source, specs } }
    | "ExportNamedDeclaration" =>
      unless fld node "source" == .null do throw "unsupported ExportNamedDeclaration with a source"
      let decl := fld node "declaration"
      if decl == .null then
        let exps ← (← (fld node "specifiers").getArr?).mapM fun sp => do
          let exported ← specName (fld sp "exported")
          let localName ← specName (fld sp "local")
          return (exported, localName)
        result := { result with exports := result.exports ++ exps }
      else
        let names := Lower.topDefs decl
        result := { result with
          units := result.units.push (compact decl)
          exports := names.foldl (fun acc name => acc.push (name, name)) result.exports }
    | "ExportDefaultDeclaration" => throw "unsupported ExportDefaultDeclaration"
    | "ExportAllDeclaration" => throw "unsupported ExportAllDeclaration with a source"
    | _ => result := { result with units := result.units.push (compact node) }
  return result

def bundle (input : Json) : Except String Bundle := do
  let m ← readModule input
  let imports := m.imports.foldl (fun acc d =>
    d.specs.foldl (fun acc (localName, imported) => acc.insert localName (d.source, imported)) acc)
    ({} : Std.HashMap String (String × String))
  return { units := m.units, imports, exports := m.exports }

end TranslateInput
