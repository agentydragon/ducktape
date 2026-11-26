import EsmSplit.LowerSplit.Certified

/-! The public lowering command refuses until a constructed rule proves the
strict all-outcomes CoreJS contract. -/

namespace EsmSplit.LowerSplit

def main : List String → IO UInt32 := certified

end EsmSplit.LowerSplit
