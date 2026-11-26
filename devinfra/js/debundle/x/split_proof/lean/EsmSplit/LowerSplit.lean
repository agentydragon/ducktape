import EsmSplit.LowerSplit.Certified

/-! Certify and publish a JavaScript deployment, or verify its existing files. -/

namespace EsmSplit.LowerSplit

def main : List String → IO UInt32 := certified

end EsmSplit.LowerSplit
