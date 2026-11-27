import EsmSplit.CoreJS.InteriorExportNames

/-! Source-plan linkage facts for an original local-export unit. They retain
its original ordinal, public/local pairs and the authored declaring owner;
they do not assert successful CoreJS linking or address preservation. -/

namespace CoreJS.TerminalEntryNames

/-- Every row pair is published by the module owning the original unit. -/
theorem published {B : EsmSplit.Bundle} {md : EsmSplit.Module} {row : EsmSplit.LocalExportUnit}
    (owned : row.ordinal ∈ md.stmts) (footprint : row.FootprintMatches (B.stmt row.ordinal))
    {publicName localName : EsmSplit.Name} (pair : (publicName, localName) ∈ row.names) :
    (publicName, localName) ∈ md.namedExports B :=
  InteriorExportNames.row_pair_published owned footprint pair

/-- A terminal export's local resolves to its original authored declaring
owner. It is either declared in the entry module or imported from that exact
owner. No value comparison, executable projection, or candidate execution is
used. -/
theorem bound_to_original_owner {B : EsmSplit.Bundle} {S : EsmSplit.Spec} {P : EsmSplit.Plan}
    (valid : EsmSplit.Valid B S P) {entry owner : EsmSplit.Module}
    (entryIn : entry ∈ P.mods) (ownerIn : owner ∈ P.mods)
    {row : EsmSplit.LocalExportUnit} (owned : row.ordinal ∈ entry.stmts)
    (footprint : row.FootprintMatches (B.stmt row.ordinal))
    {declOrdinal : Nat} (declOwned : declOrdinal ∈ owner.stmts)
    {publicName localName : EsmSplit.Name} (pair : (publicName, localName) ∈ row.names)
    (declared : localName ∈ (B.stmt declOrdinal).defs) :
    (publicName, localName) ∈ entry.namedExports B ∧
    S.claimants B declOrdinal = [owner.name] ∧ P.declaredIn B localName = .declared owner.name ∧
    ((∃ j ∈ entry.stmts, localName ∈ (B.stmt j).defs) ∨
      ∃ e ∈ entry.imports, localName ∈ e.2 ∧ e.1 = owner.name) :=
  InteriorExportNames.row_pair_binding valid entryIn ownerIn owned footprint declOwned pair declared


end CoreJS.TerminalEntryNames
