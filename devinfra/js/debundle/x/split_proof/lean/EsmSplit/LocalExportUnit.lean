import EsmSplit.Bundle

/-! One typed source export-list unit. The source producer supplies the exact
original ordinal and its own ordered `(public, local)` pairs; an aggregate
module export table does not establish which source unit contained a pair. -/

namespace EsmSplit

structure LocalExportUnit where
  ordinal : Nat
  names : List (Name × Name)

/-- Build a planner footprint from this source unit. `eager` records the
local names needed for linking; no CoreJS execution of the export list is
implied. The caller supplies the existing source text unchanged. -/
def LocalExportUnit.footprint (unit : LocalExportUnit) (src : String) : Stmt :=
  { src, eager := unit.names.map Prod.snd, exports := unit.names.map fun (publicName, localName) =>
      (publicName, some localName) }

/-- The footprint retains the ordered list, including repeated entries, and
the source text supplied for this particular ordinal. -/
theorem LocalExportUnit.exact_footprint {unit : LocalExportUnit} {B : Bundle} {src : String}
    (sourceUnit : B.stmt unit.ordinal = unit.footprint src) :
    (B.stmt unit.ordinal).src = src ∧
    (B.stmt unit.ordinal).eager = unit.names.map Prod.snd ∧
    (B.stmt unit.ordinal).exports = unit.names.map (fun (publicName, localName) => (publicName, some localName)) := by
  rw [sourceUnit]
  exact ⟨rfl, rfl, rfl⟩

/-- Every pair contributes its local binding as a planner use and retains its
public name in the same source unit's export metadata. -/
theorem LocalExportUnit.local_use_and_public {unit : LocalExportUnit} {B : Bundle} {src : String}
    (sourceUnit : B.stmt unit.ordinal = unit.footprint src) {publicName localName : Name}
    (pair : (publicName, localName) ∈ unit.names) :
    localName ∈ (B.stmt unit.ordinal).eager ∧
      (publicName, some localName) ∈ (B.stmt unit.ordinal).exports := by
  rw [sourceUnit]
  exact ⟨List.mem_map.mpr ⟨(publicName, localName), pair, rfl⟩,
    List.mem_map.mpr ⟨(publicName, localName), pair, rfl⟩⟩

#print axioms LocalExportUnit.local_use_and_public
#print axioms LocalExportUnit.exact_footprint

end EsmSplit
