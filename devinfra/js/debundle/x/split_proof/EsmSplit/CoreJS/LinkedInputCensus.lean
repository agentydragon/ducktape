import EsmSplit.CoreJS.FromESTree
import EsmSplit.CoreJS.LinkedInput
import EsmSplit.CoreJS.OpaqueInput
import EsmSplit.ESTreeUnits

/-! Per-unit diagnostic reports for linked lowering. -/

namespace CoreJS.LinkedInputCensus

open Lean EsmSplit

/-- Both reports use the same concrete lowering and opaque fallback. The
first concrete refusal stays available even if retaining the unit succeeds. -/
structure CensusUnit where
  ordinal : Nat
  statement : Except String Stmt
  concreteRefusal : Option String

/-- Diagnose concrete and retained lowering per executable unit. Providers are
not linked; retaining a unit here does not certify a split. -/
def censusUnits (source : Json) (profile : HostProfile := .empty) :
    Except String (Array CensusUnit) := do
  let nodes ← ESTreeUnits.nodes source
  let mut imports : List Name := []
  for j in nodes do
    if (← j.getObjValAs? String "type") == "ImportDeclaration" then
      for spec in ← j.getObjValAs? (Array Json) "specifiers" do
        imports := imports ++ [← FromESTree.identifier (← spec.getObjVal? "local")]
  let mut units := #[]
  for (j, i) in nodes.zipIdx do
    let kind ← j.getObjValAs? String "type"
    if
        ["ImportDeclaration", "ExportAllDeclaration", "EmptyStatement",
              "DebuggerStatement"].contains
          kind then
      continue
    let ast ←
      if kind == "ExportNamedDeclaration" || kind == "ExportDefaultDeclaration" then
        j.getObjVal? "declaration"
      else
        pure j
    if ast == .null then
      continue
    let lowered :=
      LinkedInput.lowerLinkedStatement profile imports i ast (kind == "ExportDefaultDeclaration")
        (kind == "ExportNamedDeclaration")
    let result :=
      match lowered with
      | .ok stmt => .ok stmt
      | .error why =>
        match OpaqueInput.unit i ast with
        | .ok stmt => .ok stmt
        | .error opaqueWhy => .error s!"{why}; opaque: {opaqueWhy}"
    let refusal :=
      match lowered with
      | .error why => some why
      | .ok _ => none
    units := units.push ⟨i, result, refusal⟩
  return units

/-- Diagnose concrete linked lowering in isolation. Providers are not linked,
and each unit records only its first refusal. -/
def concreteCensus (source : Json) (profile : HostProfile) : Except String Json := do
  let units ← censusUnits source profile
  let mut refusals : Array Json := #[]
  for unit in units do
    if let some why := unit.concreteRefusal then
      refusals :=
        refusals.push (Json.mkObj [("unit", Lean.toJson unit.ordinal), ("reason", .str why)])
  return Json.mkObj
      [("hostProfile", Lean.toJson profile.label), ("units", Lean.toJson units.size),
        ("lowering_refusals", .arr refusals)]

end CoreJS.LinkedInputCensus
