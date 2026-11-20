import Experiments.Effects
import Experiments.Translate

open Lean

/-- `translate_effects <bundle.json> <output.json> <cert.json> [--table <file>] [--premises <file>]`: `translate`, with
the footprints from the abstract interpreter of `Effects` instead of `Lower`'s syntactic scan. -/
def effectsFootprints (prem : Effects.Premises) : Translate.Footprinter := fun bundleJ units exported =>
  let (fps, r) := Effects.footprints units (Effects.importsOf bundleJ) prem exported
  (fps, Effects.premiseLines prem r)

def main (args : List String) : IO UInt32 := do
  let (premPath?, args) := Effects.takeFlag "--premises" args
  Translate.mainWith (effectsFootprints (← Effects.loadPremises premPath?)) args
