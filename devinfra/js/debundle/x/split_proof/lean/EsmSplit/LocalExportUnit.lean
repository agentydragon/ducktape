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

/-- A supplied source footprint retains this unit's exact ordered public/local
pairs and mentions every local. Name uses may be deduplicated or include
additional conservative uses. Extra uses can induce additional imports, so
this relation alone does not establish the actual graph or module order. -/
structure LocalExportUnit.FootprintMatches (unit : LocalExportUnit) (statement : Stmt) : Prop where
  exports : statement.exports = unit.names.map (fun (publicName, localName) => (publicName, some localName))
  locals : ∀ pair ∈ unit.names, pair.2 ∈ statement.eager ++ statement.lazy

/-- The unit's direct constructor is one way to satisfy the footprint
contract; callers may instead supply a deduplicated footprint. -/
theorem LocalExportUnit.footprint_matches (unit : LocalExportUnit) (src : String) :
    unit.FootprintMatches (unit.footprint src) := by
  refine ⟨rfl, ?_⟩
  intro pair member
  exact List.mem_append_left _ (List.mem_map.mpr ⟨pair, member, rfl⟩)

/-- A matched per-ordinal source unit supplies the local use and its exact
public/local pair, including when several public names share one local. -/
theorem LocalExportUnit.matched_local_use_and_public {unit : LocalExportUnit} {B : Bundle}
    (sourceUnit : unit.FootprintMatches (B.stmt unit.ordinal)) {publicName localName : Name}
    (pair : (publicName, localName) ∈ unit.names) :
    localName ∈ (B.stmt unit.ordinal).eager ++ (B.stmt unit.ordinal).lazy ∧
      (publicName, some localName) ∈ (B.stmt unit.ordinal).exports := by
  refine ⟨sourceUnit.locals (publicName, localName) pair, ?_⟩
  rw [sourceUnit.exports]
  exact List.mem_map.mpr ⟨(publicName, localName), pair, rfl⟩

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
#print axioms LocalExportUnit.footprint_matches
#print axioms LocalExportUnit.matched_local_use_and_public

end EsmSplit
