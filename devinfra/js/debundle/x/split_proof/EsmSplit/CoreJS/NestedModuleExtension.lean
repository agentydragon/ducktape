import EsmSplit.CoreJS.Eval
import EsmSplit.CoreJS.IndexedAllocatedCells

/-! A linker composition rule for one acyclic provider → dependency → consumer
path. The module order is the actual synchronous `Program.evalOrder`; the two
imports below relay one already allocated provider cell through two named
exports. This says nothing about cyclic evaluation or URL/import.meta effects:
neither behavior is represented by this CoreJS linker lemma. -/

namespace CoreJS.NestedModuleExtension

variable {W : World}

/-- A distinct three-module path in the actual reachable evaluation order.
Other modules may occur before or after it. -/
structure ReachablePath (P : Program) (provider dependency consumer : String) where
  before : List String
  after : List String
  order : P.evalOrder = before ++ [provider, dependency, consumer] ++ after
  provider_ne_dependency : provider ≠ dependency
  dependency_ne_consumer : dependency ≠ consumer
  provider_ne_consumer : provider ≠ consumer

/-- The first request in an import list that binds `localName`, expressed in
the same form used by `Program.resolveImportCell`. -/
def importFor (imports : List Import) (localName : Name) : Option (String × Name) :=
  imports.findSome? fun request =>
    (request.names.find? (fun (_, localName') => localName' == localName)).map
      fun (publicName, _) => (request.source, publicName)

private theorem resolveImportCell_local {P : Program} {frames : List (Nat × List (Name × Addr))}
    (fuel : Nat) {moduleName : String} {publicName localName : Name} {module : Module}
    {address : Addr} (found : P.find moduleName = some module)
    (exported : module.exports.lookup publicName = some localName)
    (cell : (module.scope frames).lookup localName = some address) :
    P.resolveImportCell frames (fuel + 1) moduleName publicName = .ok address := by
  simp only [Program.resolveImportCell, found, exported, cell]
  rfl

private theorem resolveImportCell_reexport {P : Program} {frames : List (Nat × List (Name × Addr))}
    (fuel : Nat) {moduleName providerName : String} {publicName localName providerPublic : Name}
    {module : Module} {address : Addr} (found : P.find moduleName = some module)
    (exported : module.exports.lookup publicName = some localName)
    (notLocal : (module.scope frames).lookup localName = none)
    (route : importFor module.imports localName = some (providerName, providerPublic))
    (providerCell : P.resolveImportCell frames fuel providerName providerPublic = .ok address) :
    P.resolveImportCell frames (fuel + 1) moduleName publicName = .ok address := by
  have route' :
    module.imports.findSome?
        (fun request =>
          (request.names.find? (fun (_, localName') => localName' == localName)).map
            fun (publicName, _) => (request.source, publicName)) =
      some (providerName, providerPublic) := by
    simpa [importFor] using route
  cases fuel with
  | zero => simp [Program.resolveImportCell] at providerCell
  | succ fuel =>
    simp only [Program.resolveImportCell, found, exported, notLocal, route']
    exact providerCell

/-- Append a dependency allocation pass after an earlier successful allocation
pass. Every binding cell allocated by the earlier pass remains present. -/
theorem prior_allocations_survive {oldTops extensionTops : List (Nat × Stmt)}
    {start prior final : State W} {oldFrames extensionFrames : List (Nat × List (Name × Addr))}
    (oldAllocation : declareTops oldTops start = .ok oldFrames prior)
    (extensionAllocation : declareTops extensionTops prior = .ok extensionFrames final) :
    ∀ frame ∈ oldFrames,
      ∀ cell ∈ frame.2,
        ∃ binding, prior.cellAt cell.2 = some binding ∧ final.cellAt cell.2 = some binding := by
  intro frame member cell cellMember
  obtain ⟨binding, present⟩ :=
    IndexedAllocatedCells.declareTops_present oldTops oldAllocation frame member cell cellMember
  exact
    ⟨binding, present,
      IndexedAllocatedCells.declareTops_preserves_cells extensionTops extensionAllocation cell.2
        binding present⟩

/-- Extend an already allocated provider with a reachable dependency that
re-exports one of its imports, then a consumer that imports and re-exports that
name. The actual linked import resolver returns the original provider address
at both stages, and allocation of the extension leaves every earlier binding
cell present.

The checked `depth` is the sum of export-table lengths used by
`Program.importCell`; requiring at least two gives the recursive resolver one
step for each relay. The three path modules are distinct and occur in the
reachable evaluation order. Cycles elsewhere, cycle semantics, and URL effects
are outside this theorem. -/
theorem nestedModuleExtension {P : Program} {frames : List (Nat × List (Name × Addr))}
    {oldTops extensionTops : List (Nat × Stmt)} {start prior final : State W}
    {oldFrames extensionFrames : List (Nat × List (Name × Addr))}
    (oldAllocation : declareTops oldTops start = .ok oldFrames prior)
    (extensionAllocation : declareTops extensionTops prior = .ok extensionFrames final)
    (topsExtension : P.tops = oldTops ++ extensionTops)
    (framesExtension : frames = oldFrames ++ extensionFrames)
    {provider dependency consumer : String}
    (reachable : ReachablePath P provider dependency consumer) (depth : Nat)
    (depth_ge_two : 2 ≤ depth)
    (exportsBound : (P.modules.map fun module => module.exports.length).sum = depth)
    {providerModule dependencyModule consumerModule : Module}
    {providerPublic providerLocal dependencyPublic dependencyLocal consumerPublic consumerLocal :
      Name}
    {address : Addr} {binding : Binding} (providerFound : P.find provider = some providerModule)
    (dependencyFound : P.find dependency = some dependencyModule)
    (consumerFound : P.find consumer = some consumerModule)
    (providerExport : providerModule.exports.lookup providerPublic = some providerLocal)
    (providerCell : (providerModule.scope frames).lookup providerLocal = some address)
    (dependencyExport : dependencyModule.exports.lookup dependencyPublic = some dependencyLocal)
    (dependencyNotLocal : (dependencyModule.scope frames).lookup dependencyLocal = none)
    (dependencyRoute :
      importFor dependencyModule.imports dependencyLocal = some (provider, providerPublic))
    (consumerExport : consumerModule.exports.lookup consumerPublic = some consumerLocal)
    (consumerNotLocal : (consumerModule.scope frames).lookup consumerLocal = none)
    (consumerRoute :
      importFor consumerModule.imports consumerLocal = some (dependency, dependencyPublic))
    (providerAllocated : prior.cellAt address = some binding) :
    P.evalOrder = reachable.before ++ [provider, dependency, consumer] ++ reachable.after ∧
      P.tops = oldTops ++ extensionTops ∧
      frames = oldFrames ++ extensionFrames ∧
      (∀ frame ∈ oldFrames,
        ∀ cell ∈ frame.2,
          ∃ value, prior.cellAt cell.2 = some value ∧ final.cellAt cell.2 = some value) ∧
      P.importCell frames dependency dependencyPublic = .ok address ∧
      P.importCell frames consumer consumerPublic = .ok address ∧
      final.cellAt address = some binding := by
  have hProviderShort :
    P.resolveImportCell frames (depth - 1) provider providerPublic = .ok address := by
    have h :=
      resolveImportCell_local (P := P) (frames := frames) (fuel := depth - 2) providerFound
        providerExport providerCell
    have heq : depth - 2 + 1 = depth - 1 := by omega
    simpa [heq] using h
  have hProviderLong : P.resolveImportCell frames depth provider providerPublic = .ok address := by
    have h :=
      resolveImportCell_local (P := P) (frames := frames) (fuel := depth - 1) providerFound
        providerExport providerCell
    have heq : depth - 1 + 1 = depth := by omega
    simpa [heq] using h
  have hDependencyShort :
    P.resolveImportCell frames depth dependency dependencyPublic = .ok address := by
    have h :=
      resolveImportCell_reexport (P := P) (frames := frames) (fuel := depth - 1) dependencyFound
        dependencyExport dependencyNotLocal dependencyRoute hProviderShort
    have heq : depth - 1 + 1 = depth := by omega
    simpa [heq] using h
  have hDependencyLong :
    P.resolveImportCell frames (depth + 1) dependency dependencyPublic = .ok address := by
    have h :=
      resolveImportCell_reexport (P := P) (frames := frames) (fuel := depth) dependencyFound
        dependencyExport dependencyNotLocal dependencyRoute hProviderLong
    have heq : depth + 1 = depth + 1 := rfl
    simpa [heq] using h
  have hConsumerLong :
    P.resolveImportCell frames (depth + 1) consumer consumerPublic = .ok address := by
    have h :=
      resolveImportCell_reexport (P := P) (frames := frames) (fuel := depth) consumerFound
        consumerExport consumerNotLocal consumerRoute hDependencyShort
    have heq : depth + 1 = depth + 1 := rfl
    simpa [heq] using h
  have hImportBound : (P.modules.map fun module => module.exports.length).sum + 1 = depth + 1 := by
    rw [exportsBound]
  refine
    ⟨reachable.order, topsExtension, framesExtension,
      prior_allocations_survive oldAllocation extensionAllocation, ?_, ?_, ?_⟩
  · simpa only [Program.importCell, hImportBound] using hDependencyLong
  · simpa only [Program.importCell, hImportBound] using hConsumerLong
  · exact
      IndexedAllocatedCells.declareTops_preserves_cells extensionTops extensionAllocation address
        binding providerAllocated

end CoreJS.NestedModuleExtension
