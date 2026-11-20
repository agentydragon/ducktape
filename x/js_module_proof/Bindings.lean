/- A small statement AST and linker. It follows Semantics.lean and Effects.lean. -/
inductive BindingStmt where
  | variable (name : String) (initial : Nat)
  | function (name target source : String)
  | reader (name source : String)
  | call (name : String)
  | record (name : String)
  deriving DecidableEq, Repr

structure BindingImport where
  dependency : Nat
  locals : List String
  reexports : List String
  deriving DecidableEq, BEq, Repr

structure BindingModule where
  imports : List BindingImport
  exports : List String
  statements : List BindingStmt
  assignment : List Nat
  deriving DecidableEq, Repr

def declaredName : BindingStmt → Option String
  | .variable name _ => some name
  | .function name _ _ => some name
  | .reader name _ => some name
  | .call _ => none
  | .record _ => none

def definitions (source : List BindingStmt) : List String := source.filterMap declaredName

def findDefinition (source : List BindingStmt) (name : String) : Option BindingStmt :=
  source.find? (fun statement => declaredName statement == some name)

def isVariable (source : List BindingStmt) (name : String) : Bool :=
  match findDefinition source name with
  | none => false
  | some statement => match statement with
    | .variable _ _ => true
    | .function _ _ _ => false
    | .reader _ _ => false
    | .call _ => false
    | .record _ => false

def isFunction (source : List BindingStmt) (name : String) : Bool :=
  match findDefinition source name with
  | none => false
  | some statement => match statement with
    | .variable _ _ => false
    | .function _ _ _ => true
    | .reader _ _ => true
    | .call _ => false
    | .record _ => false

def localDefinition (module : BindingModule) (name : String) : Bool :=
  module.statements.any (fun statement => declaredName statement == some name)

def ownsExport (modules : List BindingModule) (index : Nat) (name : String) : Bool :=
  match elementAt modules index with
  | none => false
  | some module => module.exports.contains name && localDefinition module name

def visible (modules : List BindingModule) (index : Nat) (name : String) : Bool :=
  match elementAt modules index with
  | none => false
  | some module => localDefinition module name || module.imports.any (fun i =>
      i.locals.contains name && ownsExport modules i.dependency name)

def validRefs (source : List BindingStmt) (modules : List BindingModule) (index : Nat) (statement : BindingStmt) : Bool :=
  match statement with
  | .variable _ _ => true
  | .function _ target sourceName =>
    isVariable source target && isVariable source sourceName &&
      (match elementAt modules index with | none => false | some m => localDefinition m target) &&
      visible modules index sourceName
  | .reader _ sourceName => isVariable source sourceName && visible modules index sourceName
  | .call name => isFunction source name && visible modules index name
  | .record name => isVariable source name && visible modules index name

def moduleValid (source : List BindingStmt) (modules : List BindingModule) (index : Nat) (module : BindingModule) : Bool :=
  let imported := module.imports.flatMap (fun i => i.locals)
  let locals := module.statements.filterMap declaredName
  let names := imported ++ locals
  decide (module.assignment.mapM (elementAt source) = some module.statements) &&
    module.exports.all (localDefinition module) &&
    module.imports.all (fun i =>
      i.locals.all (ownsExport modules i.dependency) &&
      i.reexports.all (ownsExport modules i.dependency)) &&
    names.all (fun name => names.count name == 1) &&
    module.statements.all (validRefs source modules index)

def publicNames (module : BindingModule) : List String :=
  module.exports ++ module.imports.flatMap (fun i => i.reexports)

/- A declaration's identity is its index in the original statement list.
   Resolve against the extracted module statements and imports, not the
   generator or the source-side lookup. -/
def sourceDeclaration (source : List BindingStmt) (name : String) : Option (Nat × BindingStmt) :=
  ((List.range source.length).zip source).find? (fun pair => declaredName pair.2 == some name)

def localDeclaration (module : BindingModule) (name : String) : Option (Nat × BindingStmt) :=
  (module.assignment.zip module.statements).find? (fun pair => declaredName pair.2 == some name)

def resolveReference (modules : List BindingModule) (index : Nat) (name : String) : Option (Nat × (Nat × BindingStmt)) := do
  let module ← elementAt modules index
  match localDeclaration module name with
  | some declaration => some (index, declaration)
  | none => module.imports.findSome? (fun imported => do
      if !imported.locals.contains name then none else
        let owner ← elementAt modules imported.dependency
        if !owner.exports.contains name then none else
          (localDeclaration owner name).map (fun declaration => (imported.dependency, declaration)))

def referenceNames : BindingStmt → List String
  | .variable _ _ => []
  | .function _ target source => [target, source]
  | .reader _ source => [source]
  | .call name => [name]
  | .record name => [name]

def referenceMatches (source : List BindingStmt) (modules : List BindingModule) (index : Nat) (name : String) : Bool :=
  let resolved := resolveReference modules index name
  resolved.isSome && decide (resolved.map Prod.snd = sourceDeclaration source name)

def allReferencesMatch (source : List BindingStmt) (modules : List BindingModule) : Bool :=
  (List.range modules.length).all (fun index =>
    match elementAt modules index with
    | none => false
    | some module => module.statements.all (fun statement =>
        (referenceNames statement).all (referenceMatches source modules index)))

def modulesValid (source : List BindingStmt) (sourceExports : List String)
    (modules : List BindingModule) (entry : Nat) : Bool :=
  let names := definitions source
  let assigned := modules.flatMap (fun module => module.assignment)
  let expectedPublic := sourceExports
  let exports := (elementAt modules entry).map publicNames
  names.all (fun name => names.count name == 1) &&
    expectedPublic.all (isFunction source) &&
    (match exports with
      | none => false
      | some actual => actual.length == expectedPublic.length &&
        expectedPublic.all (fun name => actual.count name == 1)) &&
    (List.range modules.length).all (fun index =>
      match elementAt modules index with
      | none => false
      | some module => moduleValid source modules index module) &&
    assigned.all (fun index => index < source.length) &&
    (List.range source.length).all (fun index => assigned.count index == 1) &&
    allReferencesMatch source modules

/- Accepted references resolve to a concrete output owner and the same
   source-indexed declaration. This is a theorem about the restricted AST and
   linker; the extraction of JavaScript into this AST remains trusted. -/
theorem binding_and_left (a b : Bool) (h : (a && b) = true) : a = true := by
  cases a <;> cases b <;> cases h <;> rfl

theorem binding_and_right (a b : Bool) (h : (a && b) = true) : b = true := by
  cases a <;> cases b <;> cases h <;> rfl

theorem binding_all_member {α : Type} (predicate : α → Bool) (items : List α)
    (item : α) (all : items.all predicate = true) (member : item ∈ items) : predicate item = true := by
  induction items with
  | nil => cases member
  | cons first rest ih =>
    change (predicate first && rest.all predicate) = true at all
    cases member with
    | head => exact binding_and_left _ _ all
    | tail _ member => exact ih (binding_and_right _ _ all) member

theorem accepted_reference_resolves (source : List BindingStmt) (exports : List String)
    (modules : List BindingModule) (entry index : Nat) (module : BindingModule)
    (statement : BindingStmt) (name : String)
    (accepted : modulesValid source exports modules entry = true)
    (inRange : index ∈ List.range modules.length)
    (moduleAt : elementAt modules index = some module)
    (inModule : statement ∈ module.statements)
    (isReference : name ∈ referenceNames statement) :
    (resolveReference modules index name).isSome = true ∧
      (resolveReference modules index name).map Prod.snd = sourceDeclaration source name := by
  have allMatch : allReferencesMatch source modules = true := binding_and_right _ _ accepted
  unfold allReferencesMatch at allMatch
  have atIndex := binding_all_member _ _ index allMatch inRange
  rw [moduleAt] at atIndex
  have atStatement := binding_all_member _ _ statement atIndex inModule
  have atName := binding_all_member _ _ name atStatement isReference
  unfold referenceMatches at atName
  exact ⟨binding_and_left _ _ atName, of_decide_eq_true (binding_and_right _ _ atName)⟩

theorem accepted_reference_owner (source : List BindingStmt) (exports : List String)
    (modules : List BindingModule) (entry index : Nat) (module : BindingModule)
    (statement : BindingStmt) (name : String)
    (accepted : modulesValid source exports modules entry = true)
    (inRange : index ∈ List.range modules.length)
    (moduleAt : elementAt modules index = some module)
    (inModule : statement ∈ module.statements)
    (isReference : name ∈ referenceNames statement) :
    ∃ owner declaration, resolveReference modules index name = some (owner, declaration) ∧
      sourceDeclaration source name = some declaration := by
  have resolved := accepted_reference_resolves source exports modules entry index module statement name
    accepted inRange moduleAt inModule isReference
  cases h : resolveReference modules index name with
  | none => rw [h] at resolved; cases resolved.1
  | some result =>
    cases result with
    | mk owner declaration =>
      refine ⟨owner, declaration, rfl, ?_⟩
      rw [h] at resolved
      exact resolved.2.symm

def asEffectModules (modules : List BindingModule) : List EffectModule :=
  modules.map (fun module =>
    ⟨module.imports.map BindingImport.dependency, [], [], module.assignment.map toString⟩)

/- Top-level calls discard the result, while exported calls record either a
   returned number or undefined separately from host-recorded events. -/
def functionOperations (source : List BindingStmt) (name : String) (observeReturn : Bool) : Option (List Operation) :=
  match findDefinition source name with
  | none => none
  | some statement => match statement with
    | .function _ target sourceName =>
      some (if observeReturn then [.addBinding target sourceName, .returnUndefined] else [.addBinding target sourceName])
    | .reader _ sourceName => some [if observeReturn then .returnBinding sourceName else .readBinding sourceName]
    | .variable _ _ => none
    | .call _ => none
    | .record _ => none

def bindingOperations (source : List BindingStmt) (statement : BindingStmt) : Option (List Operation) :=
  match statement with
  | .variable name initial => some [.declareBinding name initial]
  | .function _ _ _ => some []
  | .reader _ _ => some []
  | .record name => some [.observeBinding name]
  | .call name => functionOperations source name false

def runBindingStatements (source statements : List BindingStmt) : Option (ExecutionState Unit) := do
  let operations ← statements.mapM (bindingOperations source)
  runExecution ⟨[], [], [], none, []⟩ operations.flatten

/- Binding allocation order is not observable in this subset. Compare cells
   in source declaration order, so independent module ordering is permitted. -/
def canonicalBindingState (source : List BindingStmt) (state : ExecutionState Unit) : Option (ExecutionState Unit) := do
  let names := source.filterMap (fun statement => match statement with
    | .variable name _ => some name
    | .function _ _ _ => none
    | .reader _ _ => none
    | .call _ => none
    | .record _ => none)
  let bindings ← names.mapM (fun name => (lookupBinding state.bindings name).map (fun value => (name, value)))
  pure { state with bindings }

def sourceBindingState (source : List BindingStmt) : Option (ExecutionState Unit) :=
  (runBindingStatements source source).bind (canonicalBindingState source)

def findAssignedStatement (modules : List BindingModule) (index : Nat) : Option BindingStmt :=
  (modules.flatMap (fun module => module.assignment.zip module.statements)).findSome? (fun pair =>
    if pair.1 == index then some pair.2 else none)

def outputDefinitions (modules : List BindingModule) : List BindingStmt :=
  modules.flatMap BindingModule.statements

def outputBindingState (source : List BindingStmt) (modules : List BindingModule) (entry : Nat) : Option (ExecutionState Unit) := do
  let order ← machineTrace (asEffectModules modules) entry
  let indices ← order.mapM (fun label => (List.range source.length).find? (fun index => toString index == label))
  let statements ← indices.mapM (findAssignedStatement modules)
  let state ← runBindingStatements (outputDefinitions modules) statements
  canonicalBindingState source state

def publicFunctionTable (statements : List BindingStmt) (names : List String) : Option (List (String × List Operation)) :=
  names.mapM (fun name => (functionOperations statements name true).map (fun ops => (name, ops)))

def runExportedCalls (table : List (String × List Operation)) (state : ExecutionState Unit) (calls : List String) : Option (ExecutionState Unit) := do
  let operations ← calls.mapM (fun name => (table.find? (fun entry => entry.1 == name)).map Prod.snd)
  runExecution state operations.flatten

theorem binding_calls_preserved (left right : Option (ExecutionState Unit))
    (leftTable rightTable : Option (List (String × List Operation)))
    (same : left = right) (sameTable : leftTable = rightTable) (calls : List String) :
    left.bind (fun state => leftTable.bind (fun table => runExportedCalls table state calls)) =
      right.bind (fun state => rightTable.bind (fun table => runExportedCalls table state calls)) := by
  rw [same, sameTable]
