import EsmSplit.CoreJS.LinkedInput
import EsmSplit.CoreJS.LinkedBehavior
import EsmSplit.CoreJS.LinkWorld
import EsmSplit.CoreJS.LinkedPivotCheck
import EsmSplit.CoreJS.StateMatch
import EsmSplit.CoreJS.SyntaxMatch

/-! A concrete, checked CoreJS link boundary for one closed, unchanged provider
and one importing source entry. The checker starts from a source/candidate pair
and checked link witnesses. A publication caller can pass the result of a
source/placement constructor as the candidate; construction and equality with
the independently decoded files remain separate obligations. This module checks
CoreJS module labels and live cells, not whether relative path spelling resolves
to those labels. It adds no module-loader rules. -/

namespace CoreJS.StaticProviderLinking

open LinkedBehavior

structure ImportCell where
  importer : String
  exported : Name
  localName : Name
  address : Option Addr
  deriving DecidableEq, Repr

/-- Concrete importer aliases to one provider as found in a checked linked list. -/
def importCells (linked : List (Module × Env)) (providerName : String) : List ImportCell :=
  linked.flatMap fun (module, env) =>
    (module.imports.filter fun request => request.source == providerName).flatMap fun request =>
      request.names.map fun (exported, localName) =>
        ⟨module.name, exported, localName, env.vars.lookup localName⟩

def importsResolveTo (aliases : List ImportCell) (exported : Name) (address : Addr) : Bool :=
  aliases.all fun alias => alias.exported == exported && alias.address == some address

def providerOccurrences (linked : List (Module × Env)) (providerName : String) :=
  linked.filter fun pair => pair.1.name == providerName

private def oneProviderEntry (source candidate : Program) (providerName : String) : Bool :=
  providerName != source.entry && source.modules.length == 2 &&
    source.modules.any (·.name == source.entry) && source.modules.any (·.name == providerName) &&
    candidate.modules.any (·.name == candidate.entry) &&
    !source.modules.any (·.name == ":corejs:template-objects") &&
    !candidate.modules.any (·.name == ":corejs:template-objects")

private structure UniqueModule (modules : List Module) (name : String) where
  declaration : Module
  occurrences : modules.filter (fun module => module.name == name) = [declaration]

private def findUniqueModule (modules : List Module) (name : String) :
    Except String (UniqueModule modules name) := do
  match h : modules.filter (fun module => module.name == name) with
  | [] => throw s!"module {name} is not declared"
  | [declaration] => return ⟨declaration, h⟩
  | _ :: _ :: _ => throw s!"module {name} is declared more than once"

private def sameModule (left right : Module) : Option (PLift (left = right)) := do
  let ⟨programsEqual⟩ ← SyntaxMatch.programEq
    { entry := "provider-check", modules := [left] }
    { entry := "provider-check", modules := [right] }
  have modulesEqual : [left] = [right] := by
    change [left] = [right]
    exact congrArg Program.modules programsEqual
  return ⟨(List.cons.inj modulesEqual).1⟩

private structure ProviderOccurrence (linked : List (Module × Env)) (providerName : String) where
  provider : Module
  env : Env
  tail : List (Module × Env)
  linkedEq : linked = (provider, env) :: tail
  unique : providerOccurrences linked providerName = [(provider, env)]
  first : linked.head? = some (provider, env)

private def findProvider (linked : List (Module × Env)) (providerName : String) :
    Except String (ProviderOccurrence linked providerName) := do
  match hLinked : linked with
  | [] => throw "provider is not linked in the program"
  | head :: tail =>
    match hOccurrences : providerOccurrences linked providerName with
    | [] => throw "provider is not linked exactly once in the schedule"
    | [only] =>
      let some ⟨moduleEqual⟩ := sameModule head.1 only.1
        | throw "provider is not first in the linked schedule"
      let some envEqual := if h : head.2 = only.2 then some (PLift.up h) else none
        | throw "provider scope changed within one linked schedule"
      have pairEqual : head = only := Prod.ext moduleEqual envEqual.down
      return {
        provider := only.1
        env := only.2
        tail := tail
        linkedEq := hLinked.trans (congrArg (fun pair => pair :: tail) pairEqual)
        unique := hOccurrences
        first := by
          rw [hLinked]
          simp only [List.head?_cons]
          exact congrArg some pairEqual
      }
    | _ :: _ :: _ => throw "provider occurs more than once in the linked schedule"

private structure ProviderCell (provider : Module) (env : Env) (exported : Name) where
  localName : Name
  address : Addr
  exportExists : provider.exports.lookup exported = some localName
  cellExists : env.vars.lookup localName = some address

private def findProviderCell (provider : Module) (env : Env) (exported : Name) :
    Except String (ProviderCell provider env exported) := do
  match hExport : provider.exports.lookup exported with
  | none => throw "provider does not export the selected name"
  | some localName =>
    match hCell : env.vars.lookup localName with
    | none => throw "provider export is not linked to a local cell"
    | some address => return ⟨localName, address, hExport, hCell⟩

structure ProviderLinkCertificate (source candidate : Program) where
  before : CheckedLink source World.probe
  after : CheckedLink candidate World.probe
  providerName : String
  providerExport : Name
  providerLocal : Name
  provider : Module
  providerAddress : Addr
  sourceEntry : Module
  sourceEntryUnique : source.modules.filter (fun module => module.name == source.entry) = [sourceEntry]
  candidateEntry : Module
  candidateEntryUnique : candidate.modules.filter (fun module => module.name == candidate.entry) = [candidateEntry]
  sourceProviderUnique : source.modules.filter (fun module => module.name == providerName) = [provider]
  candidateProviderUnique : candidate.modules.filter (fun module => module.name == providerName) = [provider]
  beforeProviderEnv : Env
  afterProviderEnv : Env
  beforeHead : before.linked.head? = some (provider, beforeProviderEnv)
  afterHead : after.linked.head? = some (provider, afterProviderEnv)
  beforeUnique : providerOccurrences before.linked providerName = [(provider, beforeProviderEnv)]
  afterUnique : providerOccurrences after.linked providerName = [(provider, afterProviderEnv)]
  providerNamed : provider.name = providerName
  providerClosed : provider.imports = []
  providerExported : provider.exports.lookup providerExport = some providerLocal
  providerAddressBefore : beforeProviderEnv.vars.lookup providerLocal = some providerAddress
  providerAddressAfter : afterProviderEnv.vars.lookup providerLocal = some providerAddress
  beforeAliases : List ImportCell
  afterAliases : List ImportCell
  beforeAliasesAreActual : beforeAliases = importCells before.linked providerName
  afterAliasesAreActual : afterAliases = importCells after.linked providerName
  beforeHasImporter : beforeAliases ≠ []
  afterHasImporter : afterAliases ≠ []
  beforeAliasesResolve : importsResolveTo beforeAliases providerExport providerAddress = true
  afterAliasesResolve : importsResolveTo afterAliases providerExport providerAddress = true
  providerPrefixSame : SameUnits
    (unitsOf [(provider, beforeProviderEnv)]) (unitsOf [(provider, afterProviderEnv)])
  beforeTail : List LinkedUnit
  afterTail : List LinkedUnit
  beforeSchedule : unitsOf before.linked = unitsOf [(provider, beforeProviderEnv)] ++ beforeTail
  afterSchedule : unitsOf after.linked = unitsOf [(provider, afterProviderEnv)] ++ afterTail
  tailSameUnits? : Option (PLift (SameUnits beforeTail afterTail))
  readyEqual : before.ready = after.ready
  entryCellsMatch : exportsMatchFrom before after = true
  sameExports : SameExports source candidate

theorem ProviderLinkCertificate.before_aliases_resolve
    {source candidate : Program} (certificate : ProviderLinkCertificate source candidate)
    (alias : ImportCell) (member : alias ∈ certificate.beforeAliases) :
    alias.exported = certificate.providerExport ∧ alias.address = some certificate.providerAddress := by
  have all := List.all_eq_true.mp certificate.beforeAliasesResolve alias member
  simpa [importsResolveTo, Bool.and_eq_true, beq_iff_eq] using all

theorem ProviderLinkCertificate.after_aliases_resolve
    {source candidate : Program} (certificate : ProviderLinkCertificate source candidate)
    (alias : ImportCell) (member : alias ∈ certificate.afterAliases) :
    alias.exported = certificate.providerExport ∧ alias.address = some certificate.providerAddress := by
  have all := List.all_eq_true.mp certificate.afterAliasesResolve alias member
  simpa [importsResolveTo, Bool.and_eq_true, beq_iff_eq] using all

theorem ProviderLinkCertificate.provider_export_is_the_same_live_cell
    {source candidate : Program} (certificate : ProviderLinkCertificate source candidate) :
    certificate.provider.exports.lookup certificate.providerExport = some certificate.providerLocal ∧
      certificate.beforeProviderEnv.vars.lookup certificate.providerLocal = some certificate.providerAddress ∧
      certificate.afterProviderEnv.vars.lookup certificate.providerLocal = some certificate.providerAddress :=
  ⟨certificate.providerExported, certificate.providerAddressBefore, certificate.providerAddressAfter⟩

theorem ProviderLinkCertificate.imported_bindings_share_provider_live_cell
    {source candidate : Program} (certificate : ProviderLinkCertificate source candidate) :
    (∀ alias ∈ certificate.beforeAliases,
      alias.exported = certificate.providerExport ∧ alias.address = some certificate.providerAddress) ∧
    (∀ alias ∈ certificate.afterAliases,
      alias.exported = certificate.providerExport ∧ alias.address = some certificate.providerAddress) ∧
    certificate.provider.exports.lookup certificate.providerExport = some certificate.providerLocal ∧
    certificate.beforeProviderEnv.vars.lookup certificate.providerLocal = some certificate.providerAddress ∧
    certificate.afterProviderEnv.vars.lookup certificate.providerLocal = some certificate.providerAddress := by
  refine ⟨?_, ?_, certificate.provider_export_is_the_same_live_cell.1,
    certificate.provider_export_is_the_same_live_cell.2.1,
    certificate.provider_export_is_the_same_live_cell.2.2⟩
  · intro alias member
    exact certificate.before_aliases_resolve alias member
  · intro alias member
    exact certificate.after_aliases_resolve alias member

theorem ProviderLinkCertificate.provider_is_unique_in_both_module_lists
    {source candidate : Program} (certificate : ProviderLinkCertificate source candidate) :
    source.modules.filter (fun module => module.name == certificate.providerName) = [certificate.provider] ∧
      candidate.modules.filter (fun module => module.name == certificate.providerName) = [certificate.provider] :=
  ⟨certificate.sourceProviderUnique, certificate.candidateProviderUnique⟩

theorem ProviderLinkCertificate.entry_is_unique_in_both_module_lists
    {source candidate : Program} (certificate : ProviderLinkCertificate source candidate) :
    source.modules.filter (fun module => module.name == source.entry) = [certificate.sourceEntry] ∧
      candidate.modules.filter (fun module => module.name == candidate.entry) = [certificate.candidateEntry] :=
  ⟨certificate.sourceEntryUnique, certificate.candidateEntryUnique⟩

/-- Build typed link witnesses for one source/candidate pair. The candidate may
be a constructor result; this function does not establish where it came from.
The source graph must contain one importing entry and one provider. The provider
must be closed, unchanged, and unique in each module list and real linked
schedule. Every named import of the selected export must resolve to the provider
cell. Its before/after units, the tails, and the linked ready equality are
exposed for a separate schedule theorem. The entry's public exports must resolve
to matching live cells. -/
def certify (source candidate : Program) (pair : CheckedProbePair source candidate)
    (providerName : String) (providerExport : Name) :
    Except String (ProviderLinkCertificate source candidate) := do
  unless oneProviderEntry source candidate providerName do
    throw "expected one importing entry and one provider without template objects"
  let sourceEntry ← findUniqueModule source.modules source.entry
  let candidateEntry ← findUniqueModule candidate.modules candidate.entry
  let sourceProvider ← findUniqueModule source.modules providerName
  let candidateProvider ← findUniqueModule candidate.modules providerName
  let beforeOccurrence ← findProvider pair.before.linked providerName
  let afterOccurrence ← findProvider pair.after.linked providerName
  let some ⟨sourceProviderEqual⟩ := sameModule sourceProvider.declaration beforeOccurrence.provider
    | throw "the linked provider is not the decoded source provider"
  let some ⟨candidateProviderEqual⟩ := sameModule candidateProvider.declaration afterOccurrence.provider
    | throw "the linked provider is not the decoded candidate provider"
  let some ⟨providerEqual⟩ := sameModule beforeOccurrence.provider afterOccurrence.provider
    | throw "provider CoreJS module changed"
  let provider := beforeOccurrence.provider
  let afterProviderEqual := providerEqual.symm
  let candidateProviderIsShared := candidateProviderEqual.trans afterProviderEqual
  let providerUnitsBefore := unitsOf [(provider, beforeOccurrence.env)]
  let providerUnitsAfter := unitsOf [(provider, afterOccurrence.env)]
  let some ⟨providerPrefixSame⟩ := sameUnits? providerUnitsBefore providerUnitsAfter
    | throw "provider units do not preserve their linked identities and live cells"
  let some ⟨readyEqual⟩ := StateMatch.probe pair.before.ready pair.after.ready
    | throw "linked ready states differ"
  let .isTrue providerNamed := (inferInstance : Decidable (provider.name = providerName))
    | throw "linked provider name differs"
  let .isTrue providerClosed := (inferInstance : Decidable (provider.imports = []))
    | throw "provider is not closed"
  let beforeCell ← findProviderCell provider beforeOccurrence.env providerExport
  have afterExport : afterOccurrence.provider.exports.lookup providerExport = some beforeCell.localName :=
    afterProviderEqual ▸ beforeCell.exportExists
  let afterCell ← findProviderCell afterOccurrence.provider afterOccurrence.env providerExport
  have afterLocalEqual : afterCell.localName = beforeCell.localName :=
    Option.some.inj (afterCell.exportExists.symm.trans afterExport)
  let .isTrue hAddress := (inferInstance : Decidable (afterCell.address = beforeCell.address))
    | throw "provider export resolves to a different candidate cell"
  let beforeAliases := importCells pair.before.linked providerName
  let afterAliases := importCells pair.after.linked providerName
  let .isTrue hBeforeNonempty := (inferInstance : Decidable (beforeAliases ≠ []))
    | throw "original graph has no named imports from the provider"
  let .isTrue hAfterNonempty := (inferInstance : Decidable (afterAliases ≠ []))
    | throw "candidate graph has no named imports from the provider"
  let .isTrue hBeforeAliases :=
      (inferInstance : Decidable (importsResolveTo beforeAliases providerExport beforeCell.address = true))
    | throw "an original provider import does not resolve to the selected live cell"
  let .isTrue hAfterAliases :=
      (inferInstance : Decidable (importsResolveTo afterAliases providerExport beforeCell.address = true))
    | throw "a candidate provider import does not resolve to the selected live cell"
  let .isTrue hExports := (inferInstance : Decidable (exportsMatchFrom pair.before pair.after = true))
    | throw "entry exports do not preserve their live cells"
  have afterHead : pair.after.linked.head? = some (provider, afterOccurrence.env) := by
    simpa only [provider] using
      afterOccurrence.first.trans
        (congrArg (fun module => some (module, afterOccurrence.env)) afterProviderEqual)
  have afterUnique : providerOccurrences pair.after.linked providerName =
      [(provider, afterOccurrence.env)] := by
    simpa only [provider] using
      afterOccurrence.unique.trans
        (congrArg (fun module => [(module, afterOccurrence.env)]) afterProviderEqual)
  let beforeTail := unitsOf beforeOccurrence.tail
  let afterTail := unitsOf afterOccurrence.tail
  have beforeSchedule : unitsOf pair.before.linked = providerUnitsBefore ++ beforeTail := by
    rw [beforeOccurrence.linkedEq]
    simp [unitsOf, providerUnitsBefore, beforeTail, provider]
  have afterScheduleActual : unitsOf pair.after.linked =
      unitsOf [(afterOccurrence.provider, afterOccurrence.env)] ++ afterTail := by
    simpa [unitsOf, afterTail] using congrArg unitsOf afterOccurrence.linkedEq
  have afterProviderUnitsEqual :
      unitsOf [(afterOccurrence.provider, afterOccurrence.env)] = providerUnitsAfter := by
    simpa [providerUnitsAfter, provider] using
      congrArg (fun module => unitsOf [(module, afterOccurrence.env)]) afterProviderEqual
  have afterSchedule : unitsOf pair.after.linked = providerUnitsAfter ++ afterTail :=
    afterScheduleActual.trans (congrArg (fun preUnits : List LinkedUnit => preUnits ++ afterTail)
      afterProviderUnitsEqual)
  let tailSameUnits? := sameUnits? beforeTail afterTail
  return {
      before := pair.before
      after := pair.after
      providerName := providerName
      providerExport := providerExport
      providerLocal := beforeCell.localName
      provider := provider
      providerAddress := beforeCell.address
      sourceEntry := sourceEntry.declaration
      sourceEntryUnique := sourceEntry.occurrences
      candidateEntry := candidateEntry.declaration
      candidateEntryUnique := candidateEntry.occurrences
      sourceProviderUnique := sourceProvider.occurrences.trans
        (congrArg (fun module : Module => [module]) sourceProviderEqual)
      candidateProviderUnique := candidateProvider.occurrences.trans
        (congrArg (fun module : Module => [module]) candidateProviderIsShared)
      beforeProviderEnv := beforeOccurrence.env
      afterProviderEnv := afterOccurrence.env
      beforeHead := beforeOccurrence.first
      afterHead := afterHead
      beforeUnique := beforeOccurrence.unique
      afterUnique := afterUnique
      providerNamed := providerNamed
      providerClosed := providerClosed
      providerExported := beforeCell.exportExists
      providerAddressBefore := beforeCell.cellExists
      providerAddressAfter := by
        rw [← afterLocalEqual]
        exact hAddress ▸ afterCell.cellExists
      beforeAliases := beforeAliases
      afterAliases := afterAliases
      beforeAliasesAreActual := rfl
      afterAliasesAreActual := rfl
      beforeHasImporter := hBeforeNonempty
      afterHasImporter := hAfterNonempty
      beforeAliasesResolve := hBeforeAliases
      afterAliasesResolve := hAfterAliases
      providerPrefixSame := providerPrefixSame
      beforeTail := beforeTail
      afterTail := afterTail
      beforeSchedule := beforeSchedule
      afterSchedule := afterSchedule
      tailSameUnits? := tailSameUnits?
      readyEqual := readyEqual
      entryCellsMatch := hExports
      sameExports := pair.sameExports hExports
    }

/-! The checked input records the exact JSON source, provider files, emitted
files, authored plan, and HostProfile accepted by `decodeSingleton`. Its
assignment is part of the returned deployment. -/

structure DecodedProviderInput (profile : HostProfile) (source providers files : Lean.Json)
    (P : EsmSplit.Plan) where
  deployment : LinkedInput.DecodedDeployment
  decodedFromInput : LinkedInput.decodeSingleton source providers files P profile = .ok deployment

structure DecodedProviderCertificate {profile : HostProfile} {source providers files : Lean.Json}
    {P : EsmSplit.Plan} (input : DecodedProviderInput profile source providers files P) where
  links : ProviderLinkCertificate input.deployment.before input.deployment.after

/-- Bind link evidence to the exact pair decoded from the supplied source,
providers, emitted files, authored plan, and profile. This is link evidence
about that pair, not a publication constructor: callers must separately prove
that their source/placement constructor returns the candidate and that reread
files equal it. The checker does not decode or link again. -/
def certifyDecoded {profile : HostProfile} {source providers files : Lean.Json} {P : EsmSplit.Plan}
    (input : DecodedProviderInput profile source providers files P)
    (pair : CheckedProbePair input.deployment.before input.deployment.after) (providerName : String)
    (providerExport : Name) : Except String (DecodedProviderCertificate input) := do
  return ⟨← certify input.deployment.before input.deployment.after pair providerName providerExport⟩

theorem DecodedProviderCertificate.sameExports {profile : HostProfile} {source providers files : Lean.Json}
    {P : EsmSplit.Plan} {input : DecodedProviderInput profile source providers files P}
    (certificate : DecodedProviderCertificate input) :
    SameExports input.deployment.before input.deployment.after :=
  certificate.links.sameExports

theorem certifyDecoded_sameExports {profile : HostProfile} {source providers files : Lean.Json}
    {P : EsmSplit.Plan} {input : DecodedProviderInput profile source providers files P}
    {pair : CheckedProbePair input.deployment.before input.deployment.after}
    {providerName : String} {providerExport : Name}
    {certificate : DecodedProviderCertificate input}
    (_accepted : certifyDecoded input pair providerName providerExport = .ok certificate) :
    SameExports input.deployment.before input.deployment.after :=
  certificate.sameExports


end CoreJS.StaticProviderLinking
