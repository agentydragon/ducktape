import EsmSplit.LowerSplit.Certified

/-! The public dispatcher requires an all-outcomes, strict source-to-output
certificate. Until a constructed rule covers that contract, it refuses without
publishing files. -/

namespace EsmSplit.LowerSplit

def main : List String → IO UInt32 := certified

end EsmSplit.LowerSplit
