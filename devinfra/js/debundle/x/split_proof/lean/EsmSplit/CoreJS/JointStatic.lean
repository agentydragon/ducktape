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

/-- The decoder checks source placement, emitted syntax, capture resolution,
and (with a snapshot) loader capabilities. One pair of entry-rooted schedules
relates all normal final states under the returned stable-intrinsic premises. -/
def DeploymentEquivalent (entry : String) (parts : List LinkedInput.StaticSplit) (shared : Lean.Json)
    (inputs : Option LinkedDeployment.Input) (profile : HostProfile) (offered used : List StableIntrinsic) : Prop :=
  used ⊆ offered ∧ ∃ hints before after paths,
    LinkedInput.decodeDeployment entry parts shared inputs profile hints = .ok (before, after) ∧
    LinkedInput.publicPaths parts shared = .ok paths ∧
    NormalEquivalent (StableIntrinsic.Worlds used) before after ∧ SameExports before after ∧
    SamePublicCells paths before after

abbrev DeploymentResult (entry : String) (parts : List LinkedInput.StaticSplit) (shared : Lean.Json)
    (inputs : Option LinkedDeployment.Input) (profile : HostProfile) (offered : List StableIntrinsic) :=
  (used : List StableIntrinsic) × StructuralProof × PLift (DeploymentEquivalent entry parts shared inputs profile offered used)

private def certifyGraph (entry : String) (parts : List LinkedInput.StaticSplit) (shared : Lean.Json)
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
    | .ok (before, after) =>
      match publicPaths : LinkedInput.publicPaths parts shared with
      | .error why => throw why
      | .ok paths =>
        let a ← (checkLink before).mapError ("original linker: " ++ ·)
        let b ← (checkLink after).mapError ("candidate linker: " ++ ·)
        let ⟨cells⟩ ← checkPublicCells a.linked b.linked paths
        let used := usedIntrinsics offered before
        let (method, ⟨normal, exports⟩) ← checkLinkedSchedules used a b recordPivots
        return ⟨used, method, ⟨usedIntrinsics_subset offered before, hints, before, after, paths,
          decoded, publicPaths, normal, exports, fun w => ⟨a.forWorld w, b.forWorld w, cells⟩⟩⟩
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

def certifyDeployment (entry : String) (parts : List LinkedInput.StaticSplit) (shared : Lean.Json)
    (inputs : LinkedDeployment.Input) (profile : HostProfile) (offered : List StableIntrinsic) :
    Except String (DeploymentResult entry parts shared (some inputs) profile offered) :=
  certifyGraph entry parts shared (some inputs) profile offered

def certifyStaticDeployment (entry : String) (parts : List LinkedInput.StaticSplit) (shared : Lean.Json)
    (profile : HostProfile) (offered : List StableIntrinsic) :
    Except String (DeploymentResult entry parts shared none profile offered) :=
  certifyGraph entry parts shared none profile offered

end CoreJS.LinkedBehavior
