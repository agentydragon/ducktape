import EsmSplit.CoreJS.CheckedSplit
import EsmSplit.CoreJS.LinkedInput

/-! Connect the checked CoreJS construction to the output of the existing
linked decoder. The staging adapter supplies ASTs re-read from disk. -/

namespace EsmSplit.LowerSplit.CheckedArtifact

open Lean (Json)
open CoreJS CoreJS.CheckedSplit

private def noProviders := Json.mkObj []

def Sound (original files : Json) {source spec requested}
    (built : Construction source spec requested) : Prop :=
  ∃ decoded,
    LinkedInput.decodeSingleton original noProviders files built.plan = .ok decoded ∧
    decoded.before = source ∧ decoded.after = built.program ∧
    CheckedSplit.Sound decoded.before spec requested decoded.after

def verify (original files : Json) {source spec requested}
    (built : Construction source spec requested) :
    Except String (PLift (Sound original files built)) :=
  match lowered : LinkedInput.decodeSingleton original noProviders files built.plan with
  | .error reason => .error reason
  | .ok decoded => do
    let some ⟨sourceMatches⟩ := SyntaxMatch.programEq decoded.before source |
      throw "the original lowered differently during output verification"
    let ⟨outputMatches⟩ ← CheckedSplit.checkOutput built decoded.after
    have guarantee : CheckedSplit.Sound decoded.before spec requested decoded.after := by
      rw [sourceMatches]
      exact checkOutput_sound built outputMatches
    return ⟨⟨decoded, lowered, sourceMatches, outputMatches, guarantee⟩⟩

theorem verify_sound {original files source spec requested} {built : Construction source spec requested}
    {proof : PLift (Sound original files built)} (_accepted : verify original files built = .ok proof) :
    Sound original files built :=
  proof.down

#print axioms verify_sound

end EsmSplit.LowerSplit.CheckedArtifact
