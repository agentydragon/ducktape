import Experiments.IdentityFixture
import Experiments.IdentityScan
import IdentityFixture.Annotated

open EsmSplit MJS

namespace FrontendIdentityFixture

/-- Both `Annotated.units` and `IdentityFixture.program` are generated from
`identity_fixture/bundle.mjs`. The first uses the production translation
frontend; the second uses the separate source extractor and MiniJS parser.
This comparison does not prove either frontend models JavaScript correctly. -/
def derive : Option { c : IdentityCandidate // inferIdentity IdentityFixture.program 2 c.fn c.param c.argument = true } := do
  let names ← Lower.inferredIdentities Lower.identityScanFuel (IdentityFixture.Annotated.units.map IdentityScan.top)
  let c ← deriveIdentity IdentityFixture.program 2
  guard (names.contains c.val.fn)
  return c

theorem discovered : derive.isSome = true := by decide +kernel

/-- The plan proof consumes the inferred MiniJS certificate only after the
production inference has recognized the same binding on the frontend AST. -/
theorem crossing_preserves (fuel : Nat) (w0 : World) :
    IdentityFixture.plan.execOrder.foldl (fun s i => cstep IdentityFixture.program fuel i s)
      (G0 IdentityFixture.program, w0) =
    (List.range IdentityFixture.program.length).foldl (fun s i => cstep IdentityFixture.program fuel i s)
      (G0 IdentityFixture.program, w0) := by
  cases h : derive with
  | none =>
    have hdiscovered := discovered
    simp [h] at hdiscovered
  | some c =>
    exact inferred_identity_plan_preserves c.property (by decide +kernel) IdentityFixture.well_formed
      IdentityFixture.plan IdentityFixture.crossing_perm IdentityFixture.crossing_conditional fuel w0

end FrontendIdentityFixture
