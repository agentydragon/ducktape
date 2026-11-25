import EsmSplit.CoreJS.LinkedInput
import EsmSplit.CoreJS.LinkedPivotCertificate

/-! Two static splits checked in one closed graph. The second public namespace
is observed in the app's link, not by initializing the provider as an entry. -/
namespace CoreJS.LinkedBehavior

def SamePublicCells (path : String) (before after : Program) : Prop :=
  ∀ w, ∃ (a : CheckedLink before w) (b : CheckedLink after w) (xs ys : EntryExports.Bindings),
    EntryExports.resolve path a.linked = .ok xs ∧ EntryExports.resolve path b.linked = .ok ys ∧ xs.Perm ys

private def checkPublicCells (path : String) {before after : Program}
    (a : CheckedLink before World.probe) (b : CheckedLink after World.probe) :
    Except String (PLift (SamePublicCells path before after)) := do
  match ha : EntryExports.resolve path a.linked, hb : EntryExports.resolve path b.linked with
  | .ok xs, .ok ys =>
    if h : xs.Perm ys then
      return ⟨fun w => ⟨a.forWorld w, b.forWorld w, xs, ys, ha, hb, h⟩⟩
    else throw s!"{path}: {EntryExports.mismatch xs ys}"
  | _, _ => throw s!"could not resolve public interface of {path}"

/-- The checked programs come from the two source ASTs and their respective
output ASTs. Both public paths retain their cells and one app-rooted execution
relates the full normal final states. -/
def JointStaticEquivalent (app provider : LinkedInput.StaticSplit) : Prop :=
  ∃ before after, LinkedInput.decodeStaticSplits app provider = .ok (before, after) ∧
    NormalEquivalent (fun _ => True) before after ∧ SameExports before after ∧
    SamePublicCells provider.plan.entry before after

def certifyJointStatic (app provider : LinkedInput.StaticSplit) :
    Except String (PLift (JointStaticEquivalent app provider)) := do
  match decoded : LinkedInput.decodeStaticSplits app provider with
  | .error why => throw why
  | .ok (before, after) =>
    let a ← (checkLink before).mapError ("original linker: " ++ ·)
    let b ← (checkLink after).mapError ("candidate linker: " ++ ·)
    let ⟨cells⟩ ← checkPublicCells provider.plan.entry a b
    let (_, ⟨eqv, exports⟩) ← checkLinkedSchedules [] a b {}
    return ⟨⟨before, after, decoded, fun w _ => eqv w (by simp [StableIntrinsic.Worlds]), exports, cells⟩⟩

end CoreJS.LinkedBehavior
