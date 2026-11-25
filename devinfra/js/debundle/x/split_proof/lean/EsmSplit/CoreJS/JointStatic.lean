import EsmSplit.CoreJS.StructuralCertificate

/-! Link and certify one deployment from its actual root. All original public
interfaces are observed in the same link, including shared provider namespaces. -/
namespace CoreJS.LinkedBehavior

def CellsMatch (path : String) (a b : List (Module × Env)) : Prop :=
  ∃ xs ys : EntryExports.Bindings,
    EntryExports.resolve path a = .ok xs ∧ EntryExports.resolve path b = .ok ys ∧ xs.Perm ys

private def checkPublicCells (a b : List (Module × Env)) : (paths : List String) →
    Except String (PLift (∀ path ∈ paths, CellsMatch path a b))
  | [] => pure ⟨by simp⟩
  | path :: rest => do
    match ha : EntryExports.resolve path a, hb : EntryExports.resolve path b with
    | .ok xs, .ok ys =>
      if h : xs.Perm ys then
        let ⟨tail⟩ ← checkPublicCells a b rest
        return ⟨by
          intro name member
          rcases List.mem_cons.mp member with rfl | later
          · exact ⟨xs, ys, ha, hb, h⟩
          · exact tail name later⟩
      else throw s!"{path}: {EntryExports.mismatch xs ys}"
    | _, _ => throw s!"could not resolve public interface of {path}"

def SamePublicCells (paths : List String) (before after : Program) : Prop :=
  ∀ w, ∃ (a : CheckedLink before w) (b : CheckedLink after w),
    ∀ path ∈ paths, CellsMatch path a.linked b.linked

/-- The resolved assignment covers the decoded executable units in the
actual output modules. The entry-rooted schedules agree at the stated level
under the returned stable-intrinsic premises. -/
def DeploymentEquivalent (entry : String) (parts : List LinkedInput.StaticSplit) (shared : Lean.Json)
    (inputs : Option LinkedDeployment.Input) (profile : HostProfile) (offered used : List StableIntrinsic) (level : Guarantee) : Prop :=
  used ⊆ offered ∧ ∃ hints deployment paths,
    LinkedInput.decodeDeployment entry parts shared inputs profile hints = .ok deployment ∧
    LinkedInput.publicPaths parts shared = .ok paths ∧
    Places deployment.assignment deployment.before deployment.after ∧
    ScheduleEquivalent level (StableIntrinsic.Worlds used) deployment.before deployment.after ∧
    SameExports deployment.before deployment.after ∧
    SamePublicCells paths deployment.before deployment.after

abbrev DeploymentResult (entry : String) (parts : List LinkedInput.StaticSplit) (shared : Lean.Json)
    (inputs : Option LinkedDeployment.Input) (profile : HostProfile) (offered : List StableIntrinsic) :=
  (used : List StableIntrinsic) × (level : Guarantee) × StructuralProof ×
    PLift (DeploymentEquivalent entry parts shared inputs profile offered used level)

def certifyDeployment (entry : String) (parts : List LinkedInput.StaticSplit) (shared : Lean.Json)
    (inputs : Option LinkedDeployment.Input) (profile : HostProfile) (offered : List StableIntrinsic) :
    Except String (DeploymentResult entry parts shared inputs profile offered) := do
  let mut primary : Std.HashMap String (List Nat) := {}
  let mut recordPivots : Std.HashSet Nat := {}
  let mut base := 0
  for part in parts do
    let ids ← pivotHints part.source part.plan
    primary := primary.insert part.plan.entry ids
    for id in ids do recordPivots := recordPivots.insert (base + id)
    base := base + (← ESTreeUnits.nodes part.source).size
  let check (table : Std.HashMap String (List Nat)) : Except String (DeploymentResult entry parts shared inputs profile offered) := do
    let hints := fun (name : String) => table[name]?.getD []
    match decoded : LinkedInput.decodeDeployment entry parts shared inputs profile hints with
    | .error why => throw why
    | .ok deployment =>
      let before := deployment.before
      let after := deployment.after
      let ⟨placed⟩ ← checkPlaces deployment.assignment before after
      match publicPaths : LinkedInput.publicPaths parts shared with
      | .error why => throw why
      | .ok paths =>
        let a ← (checkLink before).mapError ("original linker: " ++ ·)
        let b ← (checkLink after).mapError ("candidate linker: " ++ ·)
        let ⟨cells⟩ ← checkPublicCells a.linked b.linked paths
        let used := usedIntrinsics offered before
        let ⟨level, method, ⟨equivalent, exports⟩⟩ ← checkLinkedSchedules used a b recordPivots
        return ⟨used, level, method, ⟨usedIntrinsics_subset offered before, hints, deployment, paths,
          decoded, publicPaths, placed, equivalent, exports, fun w => ⟨a.forWorld w, b.forWorld w, cells⟩⟩⟩
  match check primary with
  | .ok result => return result
  | .error why =>
    if inputs.isNone then throw why
    let mut expanded := primary
    let mut retry := false
    for part in parts do
      let ids := primary[part.plan.entry]?.getD []
      let extra ← projectionHints part.source ids
      unless extra.all ids.contains do
        expanded := expanded.insert part.plan.entry (extra ++ ids)
        retry := true
    unless retry do throw why
    check expanded

end CoreJS.LinkedBehavior
