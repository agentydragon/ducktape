import EsmSplit.CoreJS.LinkedInput
import EsmSplit.ESTreeUnits

/-! Authenticate the original AST against the source and check ordinal coverage
for authored construction. Classification uses the shared linked parser. -/

namespace CoreJS.AuthenticatedSource

open Lean (Json)

/-- Executable units and standalone export rows account for separate source
ordinals. An empty export row still contributes its ordinal. -/
def sourceUnitIds (program : Program) (localExports : List EsmSplit.LocalExportUnit) : List Nat :=
  program.modules.flatMap (fun m => m.body.map Prod.fst) ++ localExports.map (·.ordinal)

def checkSourceUnitCoverage (count : Nat) (program : Program) (localExports : List EsmSplit.LocalExportUnit) :
    Except String (PLift ((sourceUnitIds program localExports).Perm (List.range count))) := do
  let ids := sourceUnitIds program localExports
  let expected := List.range count
  if covered : ids.Perm expected then return ⟨covered⟩
  if let some ordinal := expected.find? (!ids.contains ·) then
    throw s!"unsupported omitted source node {ordinal}"
  throw "executable units and local export rows do not cover each source ordinal once"

/-- The indexed source retains the linked decoder's CoreJS classification and
the original node count used by source-unit coverage. -/
structure IndexedSource extends LinkedInput.DecodedSource where
  coverage : (sourceUnitIds program localExports).Perm (List.range nodes.size)

/-- Enforce indexed construction coverage on a classification that has already
been decoded. This preserves the exact refusal and does not parse or lower the
source a second time. -/
def checkIndexedSource (decoded : LinkedInput.DecodedSource) : Except String IndexedSource := do
  unless decoded.source.imports.isEmpty do throw "checked construction requires an import-free original"
  unless !decoded.hasTemplates do throw "checked construction does not install template objects"
  let ⟨coverage⟩ ← checkSourceUnitCoverage decoded.nodes.size decoded.program decoded.localExports
  return { toDecodedSource := decoded, coverage := coverage }

/-- Decode the CoreJS source and complete source-item trace once, before
construction-specific coverage is applied. -/
def decodeSourceClassification (original : Json) (sourceText entry : String) (importer := "bundle.mjs") :
    Except String LinkedInput.DecodedSource := do
  let nodes ← ESTreeUnits.checkedNodes original sourceText
  LinkedInput.decodeSingletonSourceNodes nodes entry importer

/-- Retain the full source ordinals for an authored constructor. A node the
linked model skips, such as a debugger or empty statement, prevents coverage. -/
def decodeIndexedSource (original : Json) (sourceText entry : String) : Except String IndexedSource := do
  checkIndexedSource (← decodeSourceClassification original sourceText entry)

end CoreJS.AuthenticatedSource
