import Experiments.FixtureRender
import Experiments.ProofFixture
import EsmSplit.Cli

open EsmSplit

/-- Run the same checker and writer used by the example renderer. -/
def main (args : List String) : IO UInt32 := do
  let (dir, spec, force) ← match args with
    | [dir] => pure (dir, Example.spec, false)
    | [dir, "--bad"] => pure (dir, Example.control_bad, false)
    | [dir, "--bad", "--force"] => pure (dir, Example.control_bad, true)
    | _ =>
      IO.eprintln "usage: proof_fixture_render <dir> [--bad [--force]]"
      return 2
  let plan := mkPlan Example.bundle spec
  let ok ← report Example.bundle spec plan
  if ok || force then FixtureRender.writeModules Example.bundle plan dir
  return if ok then 0 else 1
