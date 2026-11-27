import EsmSplit.LowerSplit.Certified
import EsmSplit.LowerSplit.Diagnostic

/-!
# Certify and publish a JavaScript deployment.

    lowersplit <deployment.json> <outdir> [--require LEVEL] [--check-output]
    lowersplit --diagnostic <deployment.json> <outdir> [--plan FILE] [--move-report FILE]

Only the compiled constructors can publish modules or verify a previously emitted tree.
Diagnostics can write report sidecars but never emit modules.
-/

namespace EsmSplit.LowerSplit

def main : List String → IO UInt32
  | "--diagnostic" :: args => diagnostic args
  | args => certified args

end EsmSplit.LowerSplit
