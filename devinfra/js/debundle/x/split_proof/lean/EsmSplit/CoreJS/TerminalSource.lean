import EsmSplit.CoreJS.ProjectedOrder
import EsmSplit.LocalExportUnit

/-! This contract starts with a CoreJS program and the original export-unit
rows supplied alongside it by the source decoder. Parsing/lowering and printing
are trusted; this check does not establish row provenance. -/

namespace CoreJS.TerminalSource

/-- A singleton CoreJS source has one terminal local-export unit that accounts
for its entire public table. This does not derive the full bundle footprint,
scope agreement, successful linking, or preservation of live cells. -/
structure Checked (input : Program × List EsmSplit.LocalExportUnit) where
  source : Module
  row : EsmSplit.LocalExportUnit
  singleton : input.1.modules = [source]
  entry : source.name = input.1.entry
  soleRow : input.2 = [row]
  fullTable : source.exports = row.names
  terminal : ProjectedOrder.TerminalOnly source row.ordinal

/-- Consume the shape returned by the source decoder without inferring a row
from the aggregate table. A second, even empty, row is rejected. -/
def check (input : Program × List EsmSplit.LocalExportUnit) : Except String (Checked input) := do
  match modules : input.1.modules, rows : input.2 with
  | [source], [row] =>
    if entry : source.name = input.1.entry then
      if table : source.exports = row.names then
        let terminal ← ProjectedOrder.checkTerminalOnly source row.ordinal
        return ⟨source, row, modules, entry, rows, table, terminal.down⟩
      else throw "local export row does not cover the module public surface"
    else throw "singleton source is not the entry module"
  | _, _ => throw "expected one source module and exactly one local export row"

/-- N's schedule interface applies directly to the checked source and the
original row ordinal. The order premise concerns the full planned graph. -/
theorem loaded_ids {input : Program × List EsmSplit.LocalExportUnit} (checked : Checked input)
    (B : EsmSplit.Bundle) (P : EsmSplit.Plan)
    (kept : ProjectedOrder.Kept (checked.source.body.map Prod.snd) P) :
    ((splitProgram (checked.source.body.map Prod.snd) B P).loaded.flatMap (·.body)).map Prod.fst =
      checked.source.body.map Prod.fst :=
  ProjectedOrder.terminal_loaded_ids checked.source checked.row.ordinal B P checked.terminal kept

#print axioms check
#print axioms loaded_ids

end CoreJS.TerminalSource
