namespace CoreJS.LinkedBehavior

/-- Stable categories for known limits of the compiled structural checker. -/
inductive StructuralRefusalCode where
  | nonLocalAllocation
  | movedRegionAccess
  | opaqueUnitContext
  | unsupportedSparseArrayHole
  deriving Repr, DecidableEq

def StructuralRefusalCode.label : StructuralRefusalCode → String
  | .nonLocalAllocation => "structural_non_local_allocation"
  | .movedRegionAccess => "structural_moved_region_access"
  | .opaqueUnitContext => "structural_opaque_unit_context"
  | .unsupportedSparseArrayHole => "structural_unsupported_sparse_array_hole"

/-- The source text and optional stable category travel together through the
structural decoder and checker. Unclassified failures still retain their text. -/
inductive StructuralRefusal where
  | unclassified (reason : String)
  | coded (code : StructuralRefusalCode) (reason : String)
  deriving Repr, Inhabited

def StructuralRefusal.reason : StructuralRefusal → String
  | .unclassified reason | .coded _ reason => reason

def StructuralRefusal.reasonCode : StructuralRefusal → Option String
  | .unclassified _ => none
  | .coded code _ => some code.label

def StructuralRefusal.mapReason (f : String → String) : StructuralRefusal → StructuralRefusal
  | .unclassified reason => .unclassified (f reason)
  | .coded code reason => .coded code (f reason)

end CoreJS.LinkedBehavior
