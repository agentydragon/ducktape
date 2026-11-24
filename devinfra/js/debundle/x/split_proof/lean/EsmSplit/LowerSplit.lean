import EsmSplit.LowerSplit.Certified
import EsmSplit.LowerSplit.Diagnostic

/-!
# AST + spec in; modules, verdict and a kernel certificate out.

    lowersplit <bundle.mjs|bundle.json> <spec.json> <outdir> [--require <all-outcomes|normal-completion>] [--resolved-placement CHUNK] [--check-output] [--host-contract PROFILE.json] [--structural [--deployment-origin ORIGIN]]
    lowersplit --diagnostic <bundle.mjs|bundle.json> <spec.json> <outdir> [--lean <Generated.lean> [--namespace NAME] [--variant SPEC]... [--control SPEC]...] [--replicate <K>] [--premises <file> | --footprints <file>] [--plan <file> [--all-conflicts]] [--corejs <program.json>] [--move-report <file>] [--force]

JavaScript source and annotated ESTree dumps share the Acorn parser and scope/literal metadata.
Certified mode checks the requested CoreJS guarantee before publication.
With --check-output, it reparses the existing tree before running the proof.
Lean computes the footprints
(`Effects.lean`, under the premises in the file, if any), plans the split, runs the checks, writes the modules, and with `--lean` writes a
Lean file whose `by decide +kernel` makes the kernel certify the same facts.
The bundle may import from outside modules; those are loaded first, in their order, as in the bundle.
`--force` writes the modules even when a check rejects the split, to see what Node does with them.
`--plan` writes the plan beside the modules, as JSON: `{"order": […], "modules": {name: [statement, …]}}` has the order
in which the modules run (`Plan.evalOrder`) and the statements of each module, by their numbers in the bundle.
With `--all-conflicts`, the plan includes every inverted structural conflict as an [earlier, later] pair.
Lean lowers supported annotated ASTs directly to CoreJS. For bundles outside that subset,
`--corejs` supplies the external lowering. The verdict reports which input it used.
`--move-report` writes diagnostic statement categories and up to 64 failing inverted pairs in original unit IDs.
It does not certify a split: the verdict still requires all frontend and plan premises.
`--replicate K` is for timing: K renamed copies of the bundle (the spec must cover K times as many statements).
-/

namespace EsmSplit.LowerSplit

def main : List String → IO UInt32
  | "--diagnostic" :: args => diagnostic args
  | args => certified args

end EsmSplit.LowerSplit
