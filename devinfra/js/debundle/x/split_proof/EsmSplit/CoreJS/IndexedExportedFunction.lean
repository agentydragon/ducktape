import EsmSplit.CoreJS.AuthenticatedSource
import EsmSplit.CoreJS.DeploymentPlacement
import EsmSplit.CoreJS.ExportRowsOrder
import EsmSplit.CoreJS.Fragment
import EsmSplit.CoreJS.IndexedCheckedSplit
import EsmSplit.CoreJS.IndexedInlineConstructor
import EsmSplit.CoreJS.IndexedInputChecks
import EsmSplit.CoreJS.IndexedSortedScopes
import EsmSplit.CoreJS.IndexedSourceSchedule
import EsmSplit.CoreJS.LinkWorld

/-! Extract one hoisted predicate through an original standalone export row.
The helper forms a source prefix. An optional second owner holds a single
concrete function that reads the first helper. A third owner may retain adjacent
constructor initializations and a prototype write. Exact scope agreement preserves
the links to the original bindings, including
calls made after module evaluation. -/

namespace CoreJS.IndexedExportedFunction

open AuthenticatedSource (IndexedSource)
open LinkedBehavior

private def literal : Expr → Bool
  | .null | .bool _ | .num _ | .str _ => true
  | _ => false

private def predicate (names : List Name) : Expr → Bool
  | .var name => names.contains name
  | .binary .strictEq left right | .binary .strictNe left right | .logical .or left right |
    .logical .and left right => predicate names left && predicate names right
  | e => literal e

private def seeds (names : List Name) : List (Nat × Stmt) → Option (List Name)
  | [] => some names
  | (_, .decl .const name value) :: rest =>
    if names.contains name || !literal value then none else seeds (name :: names) rest
  | _ => none

-- The retained code is a bounded CoreJS fragment. In particular the parser's
-- deferred bodies and ambient operations cannot be used to justify a move.
mutual
private def entryExpr (switchBody : Bool) : Nat → Expr → Bool
  | 0, _ => false
  | n + 1, expr =>
    match expr with
    | .undef | .this | .var _ => true
    | .func f => entryFunc switchBody n f
    | .arr elems => elems.all (entryExpr switchBody n)
    | .obj props => props.all (entryObjectProperty switchBody n)
    | .get a b | .binary _ a b | .logical _ a b =>
      entryExpr switchBody n a && entryExpr switchBody n b
    | .seq a b => switchBody && entryExpr switchBody n a && entryExpr switchBody n b
    | .unary _ e => entryExpr switchBody n e
    | .cond test yes no =>
      entryExpr switchBody n test && entryExpr switchBody n yes && entryExpr switchBody n no
    | .assign target value => entryTarget switchBody n target && entryExpr switchBody n value
    | .update target _ _ => entryTarget switchBody n target
    | .call callee args | .new callee args =>
      entryExpr switchBody n callee && args.all (entryExpr switchBody n)
    | .callMethod receiver key args =>
      entryExpr switchBody n receiver && entryExpr switchBody n key &&
        args.all (entryExpr switchBody n)
    | e => literal e
-- Like Zod's options normalization, a retained copy may override static
-- own fields after CopyDataProperties. The source object is evaluated by
-- CoreJS, including its descriptors and getter effects. Defining a getter
-- on the new object, setting its prototype, and rest exclusions are outside
-- this literal fragment.
private def entryObjectProperty (switchBody : Bool) : Nat → PropDef → Bool
  | 0, _ => false
  | n + 1, .spread source [] => entryExpr switchBody n source
  | n + 1, .init (.str _) (.func (.mk _ .arrow false [] [.ret value])) =>
    entryExpr switchBody n value
  | n + 1, .init (.str _) value => entryExpr switchBody n value
  | _, _ => false
private def entryTarget (switchBody : Bool) : Nat → Target → Bool
  | 0, _ => false
  | _ + 1, .var _ => true
  | n + 1, .member receiver key => entryExpr switchBody n receiver && entryExpr switchBody n key
  | _ + 1, .frozen _ => false
private def entryStmt (switchBody : Bool) : Nat → Stmt → Bool
  | 0, _ => false
  | n + 1, stmt =>
    match stmt with
    | .expr e | .decl _ _ e | .ret e => entryExpr switchBody n e
    | .funDecl f => entryFunc switchBody n f
    | .varDecl _ => true
    | .breakScope body => switchBody && entryStmt switchBody n body
    | .brk => switchBody
    | .block body => body.all (entryStmt switchBody n)
    | .ite test yes no =>
      entryExpr switchBody n test && entryStmt switchBody n yes && entryStmt switchBody n no
    | .loop init test update body _ =>
      init.all (entryStmt switchBody n) && entryExpr switchBody n test &&
        entryExpr switchBody n update &&
        entryStmt switchBody n body
    | _ => false
private def entryFunc (switchBody : Bool) : Nat → Func → Bool
  | n + 1, .mk _ .normal _ _ body => body.all (entryStmt switchBody n)
  | _, _ => false
end

/-- The concrete statement fragment shared with the adjacent hoist rule.
It excludes deferred bodies while allowing ordinary calls to modeled values. -/
def concreteRetainedStatement (stmt : Stmt) (switchBody : Bool := false) : Bool :=
  entryStmt switchBody 128 stmt

/-- One owner can retain adjacent constructor initializations and the eager
prototype write between them. Each initializer is closed; the installed
method can capture only the constructor whose prototype it modifies. -/
private def checkPrototypeBlock (owns : Nat → Bool) (reader : Name) (rows : List (Nat × Stmt)) :
    Except String (List (Nat × Stmt)) := do
  let
    (first, .decl .var ctor (.func init)) ::
      (write,
        .expr
          (.assign (.member (.get (.var receiver) (.str "prototype")) (.str _)) (.func method))) ::
      (last, .decl .var other (.func endInit)) :: rest :=
    rows | throw "expected two constructor initializations with a prototype write between them"
  unless owns first && owns write && owns last do
    throw "one module must own the constructors and their prototype write"
  unless
    receiver == ctor && ctor != other && entryFunc false 128 init && init.free.isEmpty &&
      entryFunc false 128 method &&
      method.free.all (· == ctor) &&
      entryFunc false 128 endInit &&
      endInit.free.isEmpty do
    throw "expected closed constructors and a method on the original prototype"
  -- A trailing hoisted location routine may live beside the constructor
  -- it uses. Its other capture is the already checked reader binding.
  if let (id, .funDecl locate) :: tail := rest then
    if owns id then
      let reads := locate.free
      unless entryFunc false 128 locate && reads.contains ctor && reads.contains reader &&
          reads.all (fun name => name == ctor || name == reader) do
        throw "the grouped location routine must capture only the constructor and the reader"
      return tail
  return rest

private def checkFamily (source : Module) (B : EsmSplit.Bundle) (request : EsmSplit.Spec)
    (helper : String) (readerOwner prototypeOwner : Option String) : Except String Unit := do
  let authored owner (row : Nat × Stmt) := request.claimants B row.1 == [owner]
  let (prefixRows, remaining) := source.body.span (authored helper)
  unless !prefixRows.isEmpty do
    throw "predicate and captures must be authored in the helper, before the entry body"
  let (_, .funDecl (.mk name .normal false [param] [.ret value])) :: revSeeds :=
    prefixRows.reverse | throw "expected an ordinary one-parameter return predicate"
  let some captures := seeds [] revSeeds.reverse | throw "expected only literal const captures"
  unless
    !captures.contains name && !captures.contains param && param != name &&
      predicate (param :: captures) value do
    throw
        "expected strict comparisons and logical expressions over the parameter and literal captures"
  unless (source.exports.map Prod.snd).contains name do
    throw "the source export must name the original predicate"
  let (afterReader, readerName) ←
    match readerOwner with
    | none =>
      pure (remaining, none)
    | some owner =>
      let (id, .funDecl (.mk reader .normal false params body)) :: rest :=
        remaining | throw "expected a hoisted reader immediately after the predicate prefix"
      let f := Func.mk reader .normal false params body
      unless authored owner (id, .funDecl f) do
        throw "the second helper must own the reader"
      let reads := f.free
      unless entryFunc false 128 f && reads.contains name && reads.all (· == name) do
        throw "the second helper must be concrete and capture only the predicate"
      pure (rest, some reader)
  let entryRows ←
    match prototypeOwner with
    | none =>
      pure afterReader
    | some owner =>
      let some reader := readerName | throw "a prototype module requires the ordered reader"
      checkPrototypeBlock (fun id => request.claimants B id == [owner]) reader afterReader
  unless entryRows.all (authored request.entry) do
    throw "remaining executable units must be authored in the entry"
  for (id, stmt) in entryRows do
    unless entryStmt false 128 stmt do
      throw s!"entry unit {id}: expected concrete retained code"

/-- Admit an initial run of hoisted functions and plain class allocations. The
tail remains in the entry. Both allocation and any later calls are still
checked against their original order and linked environments. -/
private def checkClassPrefix (source : Module) (B : EsmSplit.Bundle) (request : EsmSplit.Spec)
    (owner : String) (ordinal : Nat) : Except String Unit := do
  let owns module (row : Nat × Stmt) := request.claimants B row.1 == [module]
  let (front, tail) := source.body.span (owns owner)
  unless !front.isEmpty && !tail.isEmpty && tail.all (owns request.entry) &&
      source.body.all (fun row => row.1 < ordinal) do
    throw "expected an ordered class prefix, retained entry tail, and terminal publication"
  let mut parents : List Name := []
  let mut hasDerived := false
  for (_, stmt) in front do
    match stmt with
    | .funDecl _ => pure ()
    | .decl .var name (.cls _ false _ members) =>
      unless plainClassMembers members do
        throw "prefix class has eager members"
      parents := name :: parents
    | .decl .var _ (.derived _ false (.var parent) members none) =>
      unless parents.contains parent && plainClassMembers members do
        throw "prefix subclass must extend an earlier plain class without eager members"
      hasDerived := true
    | _ => throw "expected hoisted functions and plain class allocations in the prefix"
  unless hasDerived do
    throw "expected a derived class in the prefix"

/-- Call a public cell after a fresh module evaluation. Failure, fuel and
the final call state are retained. The receiver of the imported call is undefined. -/
def runExportCall (w : World) (initFuel callFuel : Nat) (program : Program) (publicName : Name)
    (arguments : List Val) : Res w Val :=
  match runProgram w initFuel program with
  | .ok _ state =>
    (do
        let some addr :=
          (LinkedBehavior.exports w program).bind
            (·.lookup publicName) | throwErr .reference "missing public function"
        callFn callFuel (← readCell addr) .undef arguments)
      state
  | .throw value state => .throw value state
  | .unknown why state => .unknown why state
  | .fuel => .fuel

structure Construction (decoded : IndexedSource) (B : EsmSplit.Bundle)
    (request : EsmSplit.Spec) where
  schedule : IndexedSourceSchedule.Evidence decoded B request
  program : Program
  constructed : program = splitProgramIndexed decoded.source.body B schedule.plan
  places :
    Places (IndexedCheckedSplit.assignments decoded.source.body schedule.plan) decoded.program
      program
  metadataOnce : IndexedCheckedSplit.MetadataSelectorsOnce B request decoded.localExports
  metadataOwner : ∀ row ∈ decoded.localExports, request.claimants B row.ordinal = [request.entry]
  exactExports :
    ∀ w,
      ∃ cells,
        LinkedBehavior.exports w decoded.program = some cells ∧
          LinkedBehavior.exports w program = some cells
  allOutcomes : ∀ w fuel, runProgram w fuel decoded.program = runProgram w fuel program
  linkedCaptures :
    ∀ w,
      ∃ before : CheckedLink decoded.program w,
        ∃ after : CheckedLink program w,
          before.ready = after.ready ∧
            captureUnits (unitsOf before.linked) = captureUnits (unitsOf after.linked)

private def constructOrdered (decoded : IndexedSource) (B : EsmSplit.Bundle) (request : EsmSplit.Spec)
    (classPrefix : Bool) :
    Except String (Construction decoded B request) := do
  let (helper, readerOwner, prototypeOwner) ←
    match request.order with
    | [owner] =>
      pure (owner, none, none)
    | [owner, reader] =>
      pure (owner, some reader, none)
    | [owner, reader, prototype] =>
      pure (owner, some reader, some prototype)
    | _ =>
      throw "expected a predicate module, with optional reader and prototype modules"
  let [row] := decoded.localExports | throw "expected one original standalone export row"
  let source := decoded.source
  let original := decoded.program
  unless
    request.sequential && !request.privateEntryBody &&
      decide (request.entry :: request.order).Nodup &&
      request.modules.all (fun module =>
        module.1.endsWith ".mjs" ||
          (classPrefix && module.1 == request.entry && module.1.endsWith ".js")) &&
      !decoded.hasTemplates do
    throw "expected distinct sequential .mjs modules with a public entry and no templates"
  let .isTrue metadataOnce :=
    (inferInstance :
      Decidable
        (IndexedCheckedSplit.MetadataSelectorsOnce B request
          decoded.localExports)) | throw "each original metadata row must be selected exactly once"
  let .isTrue metadataOwner :=
    (inferInstance :
      Decidable
        (∀ row ∈ decoded.localExports,
          request.claimants B row.ordinal =
            [request.entry])) | throw "standalone export row must be authored in the entry"
  if classPrefix then
    unless readerOwner.isNone && prototypeOwner.isNone do
      throw "expected one generated class prefix module"
    checkClassPrefix source B request helper row.ordinal
  else
    checkFamily source B request helper readerOwner prototypeOwner
  unless decide (IndexedInlineConstructor.SelectorsOnce source.body B request) do
    throw "each original executable unit must be selected exactly once"
  let schedule ← IndexedSourceSchedule.check decoded B request
  let P := schedule.plan
  let program := splitProgramIndexed source.body B P
  unless program.evalOrder == request.order ++ [request.entry] do
    throw "plan must follow authored module order"
  let .isTrue imports :=
    (inferInstance : Decidable (source.imports = [])) | throw "expected no source imports"
  let .isTrue sourceEntry :=
    (inferInstance : Decidable (original.entry = source.name)) | throw "wrong source entry"
  have found : original.find original.entry = some source := by
    change decoded.program.find decoded.program.entry = some decoded.source
    change decoded.program.entry = decoded.source.name at sourceEntry
    simp [Program.find, decoded.singleton, sourceEntry]
  let .isTrue sorted :=
    (inferInstance :
      Decidable
        (source.body.Pairwise fun a b => a.1 ≤ b.1)) | throw "unexpected source ordinal order"
  have kept : SparseProjectedOrder.OrderKept source.body P := by
    change SparseProjectedOrder.OrderKept decoded.source.body schedule.plan
    rw [← schedule.sequence.sourceEq]
    exact schedule.executableOrder
  let .isTrue fp :=
    (inferInstance :
      Decidable
        (IndexedInputChecks.FootprintChecks source.body
          B)) | throw "source and planner footprints differ"
  let .isTrue defs :=
    (inferInstance :
      Decidable
        (IndexedInputChecks.DefinitionChecks source.body
          B)) | throw "planner definitions lack source rows"
  -- Use the decoder's classification for the body and every metadata ordinal.
  let .isTrue table :=
    (inferInstance :
      Decidable
        (source.exports =
          decoded.localExports.flatMap
            (·.names))) | throw "only authored standalone publication is supported"
  let .isTrue rowOrder :=
    (inferInstance :
      Decidable
        (decoded.localExports.Pairwise
          (fun a b =>
            a.ordinal < b.ordinal))) | throw "standalone export rows are not in source order"
  have rows : ExportRows.SourceRows source decoded.localExports :=
    ⟨table, schedule.sequence.metadataIdsUnique, by
      simpa only [source, schedule.sequence.sourceEq] using schedule.sequence.disjoint⟩
  let .isTrue declared :=
    (inferInstance :
      Decidable
        (ExportRows.LocalsDeclared source B)) | throw "export local lacks an original declaration"
  let ⟨owner, ⟨planEntry⟩⟩ ←
    (show Except String (Σ m, PLift (P.find P.entry = some m)) from
        match P.find P.entry with
        | none => .error "missing plan entry"
        | some m => .ok ⟨m, ⟨rfl⟩⟩)
  let .isTrue matched :=
    (inferInstance :
      Decidable
        (∀ id ∈ owner.stmts,
          ExportRows.UnitMatches B decoded.localExports
            id)) | throw "entry units differ from the original metadata"
  let .isTrue noGlue :=
    (inferInstance : Decidable (owner.exports = [])) | throw "entry needs unsupported glue exports"
  have within : ∀ row ∈ decoded.localExports, row.ordinal < B.length := by
    intro row member
    exact
      List.mem_range.1
        (schedule.sequence.sourceCoverage.mem_iff.mp
          (List.mem_append_right _ (List.mem_map.mpr ⟨row, member, rfl⟩)))
  have selected :
    owner.stmts.filterMap (ExportRows.rowAt decoded.localExports) = decoded.localExports := by
    have valid := schedule.valid
    change schedule.plan.find schedule.plan.entry = some owner at planEntry
    rw [schedule.planEq] at valid planEntry
    exact ExportRows.mkPlan_select_source_rows valid planEntry rowOrder within metadataOwner
  let labels := IndexedCheckedSplit.assignments source.body P
  let ⟨places⟩ ← checkPlaces labels original program
  let before ← checkLink original
  have footprint := IndexedInputChecks.checked_footprints fp
  have complete := IndexedInputChecks.checked_definitions defs
  have ⟨tops, regions⟩ :=
    IndexedSortedScopes.indexed_tops_regions schedule.valid footprint.1 rfl decoded.singleton
      imports found sorted
  have scopes :=
    IndexedUnitsAgree.indexed_scopes_agree schedule.valid footprint.1 footprint.2 complete rfl
      imports found sorted (SparseProjectedOrder.split_indexed_loaded_eq_source kept)
  have linked (w : World) :
    ∃ after : CheckedLink program w,
      (before.forWorld w).ready = after.ready ∧
        SameUnits (unitsOf (before.forWorld w).linked) (unitsOf after.linked) ∧
        ∃ cells,
          EntryExports.resolve original.entry (before.forWorld w).linked = .ok cells ∧
            EntryExports.resolve program.entry after.linked = .ok cells := by
    obtain ⟨_, frames, _, after, allocated, beforeScopes, afterScopes, ready, same, _⟩ :=
      OrderedRegion.matching_link_with_frames regions tops scopes (before.forWorld w)
    rw [IndexedEntryLink.import_free_source_tops found imports sorted] at allocated
    obtain ⟨actual, cells, actualScopes, _, sourceTable, targetTable, _⟩ :=
      ExportRows.rows_resolved_from_plan schedule.valid footprint.1 complete allocated rfl imports
        found rows planEntry matched selected noGlue
        (fun _ => ExportRows.row_locals_declared rows declared)
    have sourceLinked :=
      Except.ok.inj
        (beforeScopes.symm.trans (IndexedEntryLink.import_free_entry_scopes found imports frames))
    have targetLinked := Except.ok.inj (afterScopes.symm.trans actualScopes)
    exact
      ⟨after, ready, same, cells, sourceLinked.symm ▸ sourceTable, targetLinked.symm ▸ targetTable⟩
  have exactExports :
    ∀ w,
      ∃ cells,
        LinkedBehavior.exports w original = some cells ∧
          LinkedBehavior.exports w program = some cells := by
    intro w
    obtain ⟨after, _, _, cells, left, right⟩ := linked w
    refine ⟨cells, ?_, ?_⟩
    · rw [LinkedBehavior.exports, (before.forWorld w).equation]
      change (EntryExports.resolve original.entry _).toOption = _
      rw [left]
      rfl
    · rw [LinkedBehavior.exports, after.equation]
      change (EntryExports.resolve program.entry _).toOption = _
      rw [right]
      rfl
  have outcomes : ∀ w fuel, runProgram w fuel original = runProgram w fuel program := by
    intro w fuel
    obtain ⟨after, ready, same, _⟩ := linked w
    rw [run_after_link fuel (before.forWorld w), run_after_link fuel after, ready,
      runUnits_same fuel same]
  -- This records the exact captured environments as well as the common
  -- initialized heap, so a retained caller uses the same hoisted callee cell.
  have captures :
    ∀ w,
      ∃ a : CheckedLink original w,
        ∃ b : CheckedLink program w,
          a.ready = b.ready ∧
            captureUnits (unitsOf a.linked) = captureUnits (unitsOf b.linked) := by
    intro w
    obtain ⟨after, ready, same, _⟩ := linked w
    exact ⟨before.forWorld w, after, ready, OrderedRegion.sameUnits_capture same⟩
  return ⟨schedule, program, rfl, places, metadataOnce, metadataOwner, exactExports, outcomes,
      captures⟩

def construct (decoded : IndexedSource) (B : EsmSplit.Bundle) (request : EsmSplit.Spec) :
    Except String (Construction decoded B request) :=
  constructOrdered decoded B request false

def constructClassPrefix (decoded : IndexedSource) (B : EsmSplit.Bundle) (request : EsmSplit.Spec) :
    Except String (Construction decoded B request) :=
  constructOrdered decoded B request true

/-- Transfer the authored placement and full outcome contract to independently
decoded files, including calls on the identical live export cell. -/
theorem Construction.readbackGuarantees {decoded B request} (built : Construction decoded B request)
    {observed : Program} (same : observed = built.program) :
    Places (IndexedCheckedSplit.assignments decoded.source.body built.schedule.plan) decoded.program
        observed ∧
      (∀ w,
        ∃ cells,
          LinkedBehavior.exports w decoded.program = some cells ∧
            LinkedBehavior.exports w observed = some cells) ∧
      (∀ w fuel, runProgram w fuel decoded.program = runProgram w fuel observed) ∧
      ∀ w initFuel callFuel name args,
        runExportCall w initFuel callFuel decoded.program name args =
          runExportCall w initFuel callFuel observed name args := by
  subst observed
  refine ⟨built.places, built.exactExports, built.allOutcomes, ?_⟩
  intro w initFuel callFuel name args
  obtain ⟨_, before, after⟩ := built.exactExports w
  unfold runExportCall
  rw [built.allOutcomes, before, after]

end CoreJS.IndexedExportedFunction
