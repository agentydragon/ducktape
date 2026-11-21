/- A small counter language, not a complete ECMAScript semantics. -/

structure Ref where
  moduleName : String
  name : String
  deriving DecidableEq, Repr

inductive Cell where
  | live
  | snapshot
  deriving DecidableEq, Repr

structure Graph where
  live : Ref
  snapshot : Option Ref
  aliases : List (Ref × Ref)
  increment : Ref
  current : Ref
  initial : Nat
  deriving DecidableEq, Repr

/- The frontend accepts only this small AST. Parsing source text into these
   records is still trusted; the relationships between names are checked here. -/
structure Export where
  localName : String
  publicName : String
  deriving DecidableEq, Repr

structure ReExport where
  moduleSpecifier : String
  localName : String
  publicName : String
  deriving DecidableEq, Repr

structure SourceAst where
  cell : String
  initial : Nat
  increment : String
  incrementRef : String
  current : String
  currentRef : String
  exports : List Export
  deriving DecidableEq, Repr

structure OutputAst where
  stateModule : String
  readerModule : String
  cell : String
  initial : Nat
  increment : String
  incrementRef : String
  importFrom : String
  importedName : String
  importedLocal : String
  snapshot : Option (String × String)
  current : String
  currentRef : String
  exports : List ReExport
  deriving DecidableEq, Repr

def analyzeSource (s : SourceAst) : Option Graph :=
  if s.cell != s.increment && s.cell != s.current && s.increment != s.current &&
      s.incrementRef == s.cell && s.currentRef == s.cell && s.exports.length == 2 &&
      s.exports.contains ⟨s.increment, s.increment⟩ &&
      s.exports.contains ⟨s.current, s.current⟩ then
    some ⟨⟨"source", s.cell⟩, none, [], ⟨"source", s.incrementRef⟩,
      ⟨"source", s.currentRef⟩, s.initial⟩
  else none

def analyzeOutput (s : SourceAst) (o : OutputAst) : Option Graph := do
  let _ ← analyzeSource s
  if o.stateModule == o.readerModule || o.increment != s.increment ||
      o.current != s.current || o.incrementRef != o.cell ||
      o.importFrom != o.stateModule || o.importedName != o.cell ||
      o.exports.length != 2 ||
      !o.exports.contains ⟨o.stateModule, s.increment, s.increment⟩ ||
      !o.exports.contains ⟨o.readerModule, s.current, s.current⟩ then
    none
  else
    let snapshot ← match o.snapshot with
      | none => some none
      | some (name, value) =>
        if name != o.importedLocal && value == o.importedLocal then
          some (some ⟨o.readerModule, name⟩)
        else none
    some ⟨⟨o.stateModule, o.cell⟩, snapshot,
      [(⟨o.readerModule, o.importedLocal⟩, ⟨o.stateModule, o.importedName⟩)],
      ⟨o.stateModule, o.incrementRef⟩, ⟨o.readerModule, o.currentRef⟩, o.initial⟩

def resolve (g : Graph) (r : Ref) : Ref :=
  match g.aliases.find? (fun pair => pair.1 == r) with
  | some pair => pair.2
  | none => r

def classify (g : Graph) (r : Ref) : Option Cell :=
  let target := resolve g r
  if target == g.live then some .live
  else if some target == g.snapshot then some .snapshot
  else none

structure Program where
  initial : Nat
  increment : Cell
  current : Cell
  deriving DecidableEq, Repr

def normalize (g : Graph) : Option Program := do
  let increment ← classify g g.increment
  let current ← classify g g.current
  pure ⟨g.initial, increment, current⟩

inductive Call where
  | increment
  | current
  deriving DecidableEq, Repr

structure State where
  live : Nat
  snapshot : Nat
  deriving DecidableEq, Repr

/- The compact counter state is a projection of the shared execution state.
   The snapshot is ghost state when the JavaScript has no snapshot. -/
def counterState (shared : ExecutionState Unit) : Option State := do
  pure ⟨← shared.cell, ← shared.snapshot⟩

def initializeCounter (initial : Nat) : Option State := do
  let shared ← runExecution (⟨[], [], [], none, [], none, []⟩ : ExecutionState Unit)
    [.initialize initial, .captureSnapshot]
  counterState shared

/- The counter subset has a fixed dependency shape: the reader depends on
   state and the entry depends on both. Run initialization through the same
   module scheduler as the effects subset. The snapshot operation initializes
   ghost state when no JavaScript snapshot exists. -/
def counterInitModule (imports : List Nat) (effects : List String) : EffectModule :=
  ⟨imports, imports, [], effects⟩

def scheduledCounterInit (initial : Nat) (modules : List EffectModule) (entry : Nat) : Option State := do
  let operations ← machineTrace modules entry
  let operations ← operations.mapM (fun operation =>
    if operation == "init" then some (.initialize initial)
    else if operation == "snapshot" then some .captureSnapshot
    else none)
  let shared ← runExecution (⟨[], [], [], none, [], none, []⟩ : ExecutionState Unit) operations
  counterState shared

def sourceCounterInit (initial : Nat) : Option State :=
  scheduledCounterInit initial [counterInitModule [] ["init", "snapshot"]] 0

def outputCounterInit (initial : Nat) : Option State :=
  scheduledCounterInit initial [counterInitModule [] ["init"], counterInitModule [0] ["snapshot"],
    counterInitModule [0, 1] []] 2

def initialState (p : Program) : State := (initializeCounter p.initial).getD ⟨0, 0⟩

def counterOperation (state : State) (operation : Operation) : State × Option Nat :=
  let shared : ExecutionState Unit := ⟨[("$live", state.live), ("$snapshot", state.snapshot)], [], [], none, [], none, []⟩
  match executeStep shared operation with
  | none => (state, none)
  | some result =>
    let next := (counterState result).getD state
    let value := match result.events with
      | [] => none
      | event :: rest =>
        match rest with
        | _ :: _ => none
        | [] => match event with
          | .number n => some n
          | .text _ => none
    (next, value)

/- Check the projection for all cell values, including the snapshot case.
   These facts depend on the shared executor rather than defining another
   counter transition. -/
theorem counter_initialize_shared (initial : Nat) :
    initializeCounter initial = some ⟨initial, initial⟩ := by rfl

theorem counter_initialization_scheduled (initial : Nat) :
    sourceCounterInit initial = initializeCounter initial ∧
    outputCounterInit initial = initializeCounter initial := by
  exact ⟨rfl, rfl⟩

theorem counter_shared_operations (state : State) :
    counterOperation state .increment = (⟨state.live + 1, state.snapshot⟩, none) ∧
    counterOperation state .incrementSnapshot = (⟨state.live, state.snapshot + 1⟩, none) ∧
    counterOperation state .readCell = (state, some state.live) ∧
    counterOperation state .readSnapshot = (state, some state.snapshot) := by
  cases state
  exact ⟨rfl, rfl, rfl, rfl⟩

def step (p : Program) (state : State) : Call → State × Option Nat
  | .increment =>
    match p.increment with
    | .live => counterOperation state .increment
    | .snapshot => counterOperation state .incrementSnapshot
  | .current =>
    match p.current with
    | .live => counterOperation state .readCell
    | .snapshot => counterOperation state .readSnapshot

def observations (p : Program) : State → List Call → List Nat
  | _, [] => []
  | state, call :: calls =>
    let (next, result) := step p state call
    match result with
    | none => observations p next calls
    | some value => value :: observations p next calls

def graphObservations (g : Graph) (calls : List Call) : Option (List Nat) :=
  (normalize g).map (fun p => observations p (initialState p) calls)

def finalState (p : Program) : State → List Call → State
  | state, [] => state
  | state, call :: calls => finalState p (step p state call).1 calls

def graphState (g : Graph) (calls : List Call) : Option State :=
  (normalize g).map (fun p => finalState p (initialState p) calls)

/- Direct execution model for the supported AST, expressed without constructing
   Graphs. Export checks and well-formedness are established separately. -/
def directSourceProgram (s : SourceAst) : Option Program :=
  if s.incrementRef == s.cell && s.currentRef == s.cell then
    some ⟨s.initial, .live, .live⟩
  else none

def directOutputProgram (o : OutputAst) : Option Program :=
  if o.incrementRef != o.cell || o.importFrom != o.stateModule || o.importedName != o.cell then
    none
  else if o.currentRef == o.importedLocal then
    some ⟨o.initial, .live, .live⟩
  else
    match o.snapshot with
    | some (name, value) =>
      if o.currentRef == name && value == o.importedLocal then
        some ⟨o.initial, .live, .snapshot⟩
      else none
    | none => none

def sourceProgram (s : SourceAst) : Option Program := (analyzeSource s).bind normalize

/- Initialization is modeled in two steps: link imports, then initialize the
   live cell and optional snapshot. The snapshot slot is ghost state when no
   snapshot is declared; this keeps the relational State representation common. -/
inductive InitPhase where
  | parsed
  | linked
  | ready
  deriving DecidableEq, Repr

structure InitMachine where
  phase : InitPhase
  live : Option Nat
  snapshot : Option Nat
  importAlias : Option (Ref × Ref)
  deriving DecidableEq, Repr

def freshMachine : InitMachine := ⟨.parsed, none, none, none⟩

def sourceInitStep (s : SourceAst) (m : InitMachine) : Option InitMachine :=
  match m.phase with
  | .parsed => some { m with phase := .linked }
  | .linked => do
    let initialized ← sourceCounterInit s.initial
    some { m with phase := .ready, live := some initialized.live, snapshot := some initialized.snapshot }
  | .ready => none

def outputInitStep (o : OutputAst) (m : InitMachine) : Option InitMachine :=
  match m.phase with
  | .parsed =>
    if o.importFrom == o.stateModule && o.importedName == o.cell then
      some { m with phase := .linked, importAlias := some (⟨o.readerModule, o.importedLocal⟩, ⟨o.stateModule, o.cell⟩) }
    else none
  | .linked =>
    match o.snapshot with
    | none => do
      let initialized ← outputCounterInit o.initial
      some { m with phase := .ready, live := some initialized.live, snapshot := some initialized.snapshot }
    | some (_, value) =>
      if value == o.importedLocal then
        do
          let initialized ← outputCounterInit o.initial
          some { m with phase := .ready, live := some initialized.live, snapshot := some initialized.snapshot }
      else none
  | .ready => none

def sourceStartup (s : SourceAst) : Option InitMachine := do
  let linked ← sourceInitStep s freshMachine
  sourceInitStep s linked

def outputStartup (o : OutputAst) : Option InitMachine := do
  let linked ← outputInitStep o freshMachine
  outputInitStep o linked

def readyState (m : InitMachine) : Option State := do
  if m.phase != .ready then none
  else pure ⟨← m.live, ← m.snapshot⟩


/- A candidate lowering is accepted only if it agrees with the direct AST
   interpretation. This is a checked sufficient condition, not completeness. -/
def outputProgram (s : SourceAst) (o : OutputAst) : Option Program := do
  let graph ← analyzeOutput s o
  let lowered ← normalize graph
  let direct ← directOutputProgram o
  let initial ← (outputStartup o).bind readyState
  if lowered == direct then
    if initial == initialState direct then some lowered else none
  else none

def sourceObservations (s : SourceAst) (calls : List Call) : Option (List Nat) :=
  (directSourceProgram s).map (fun p => observations p (initialState p) calls)

def outputObservations (s : SourceAst) (o : OutputAst) (calls : List Call) : Option (List Nat) :=
  (directOutputProgram o).map (fun p => observations p (initialState p) calls)

def sourceState (s : SourceAst) (calls : List Call) : Option State :=
  (directSourceProgram s).map (fun p => finalState p (initialState p) calls)

def outputState (s : SourceAst) (o : OutputAst) (calls : List Call) : Option State :=
  (directOutputProgram o).map (fun p => finalState p (initialState p) calls)

theorem frontend_observations_equivalent (source : SourceAst) (output : OutputAst)
    (sourceSound : directSourceProgram source = sourceProgram source)
    (outputSound : outputProgram source output = directOutputProgram output)
    (same : sourceProgram source = outputProgram source output) (calls : List Call) :
    sourceObservations source calls = outputObservations source output calls := by
  exact congrArg (fun result => result.map (fun p => observations p (initialState p) calls))
    (sourceSound.trans (same.trans outputSound))

theorem frontend_state_equivalent (source : SourceAst) (output : OutputAst)
    (sourceSound : directSourceProgram source = sourceProgram source)
    (outputSound : outputProgram source output = directOutputProgram output)
    (same : sourceProgram source = outputProgram source output) (calls : List Call) :
    sourceState source calls = outputState source output calls := by
  exact congrArg (fun result => result.map (fun p => finalState p (initialState p) calls))
    (sourceSound.trans (same.trans outputSound))

theorem extracted_graphs_equivalent (source output : Graph)
    (same : normalize source = normalize output) (calls : List Call) :
    graphObservations source calls = graphObservations output calls := by
  exact congrArg (fun result => result.map (fun p => observations p (initialState p) calls)) same

theorem extracted_graphs_state_equivalent (source output : Graph)
    (same : normalize source = normalize output) (calls : List Call) :
    graphState source calls = graphState output calls := by
  exact congrArg (fun result => result.map (fun p => finalState p (initialState p) calls)) same

def liveProgram : Program := ⟨0, .live, .live⟩
def snapshotProgram : Program := ⟨0, .live, .snapshot⟩
example : observations liveProgram (initialState liveProgram) [.increment, .current] = [1] := rfl
example : observations snapshotProgram (initialState snapshotProgram) [.increment, .current] = [0] := rfl

/- Operational calls inspect AST references on each step. The snapshot field
   in State is a ghost cell when no source-level snapshot exists. -/
def sourceOperationalStep (s : SourceAst) (state : State) : Call → Option (State × Option Nat)
  | .increment => if s.incrementRef == s.cell then some (counterOperation state .increment) else none
  | .current => if s.currentRef == s.cell then some (counterOperation state .readCell) else none

def outputOperationalStep (o : OutputAst) (state : State) (call : Call) : Option (State × Option Nat) :=
  if o.incrementRef != o.cell || o.importFrom != o.stateModule || o.importedName != o.cell then none
  else if o.currentRef == o.importedLocal then
    match call with
    | .increment => some (counterOperation state .increment)
    | .current => some (counterOperation state .readCell)
  else
    match o.snapshot with
    | some (name, value) =>
      if o.currentRef == name && value == o.importedLocal then
        match call with
        | .increment => some (counterOperation state .increment)
        | .current => some (counterOperation state .readSnapshot)
      else none
    | none => none

theorem source_step_sound (s : SourceAst) (p : Program) (state : State) (call : Call)
    (h : directSourceProgram s = some p) :
    sourceOperationalStep s state call = some (step p state call) := by
  cases ha : (s.incrementRef == s.cell) <;> cases hb : (s.currentRef == s.cell)
  · rw [directSourceProgram, ha] at h; cases h
  · rw [directSourceProgram, ha] at h; cases h
  · rw [directSourceProgram, ha, hb] at h; cases h
  · rw [directSourceProgram, ha, hb] at h
    cases h
    cases call
    · rw [sourceOperationalStep, ha]; rfl
    · rw [sourceOperationalStep, hb]; rfl

theorem output_step_sound (o : OutputAst) (p : Program) (state : State) (call : Call)
    (h : directOutputProgram o = some p) :
    outputOperationalStep o state call = some (step p state call) := by
  unfold directOutputProgram at h
  split at h
  · cases h
  · rename_i good
    split at h
    · rename_i live
      cases h
      unfold outputOperationalStep
      rw [if_neg good, if_pos live]
      cases call <;> rfl
    · rename_i notLive
      cases hs : o.snapshot with
      | none => rw [hs] at h; cases h
      | some pair =>
        cases pair with
        | mk name value =>
          rw [hs] at h
          dsimp at h
          split at h
          · rename_i snap
            cases h
            unfold outputOperationalStep
            rw [if_neg good, if_neg notLive, hs]
            dsimp
            rw [if_pos snap]
            cases call <;> rfl
          · cases h

theorem frontend_operational_step (s : SourceAst) (o : OutputAst) (p : Program)
    (sourceSound : directSourceProgram s = sourceProgram s)
    (outputSound : outputProgram s o = directOutputProgram o)
    (sourceResolved : sourceProgram s = some p)
    (outputResolved : outputProgram s o = some p)
    (state : State) (call : Call) :
    sourceOperationalStep s state call = outputOperationalStep o state call := by
  rw [source_step_sound s p state call (sourceSound.trans sourceResolved)]
  rw [output_step_sound o p state call (outputSound.symm.trans outputResolved)]

theorem source_frontend_general (s : SourceAst) (g : Graph)
    (h : analyzeSource s = some g) : directSourceProgram s = normalize g := by
  unfold analyzeSource at h
  split at h
  · rename_i cond
    cases h
    cases hi : (s.incrementRef == s.cell) <;> cases hc : (s.currentRef == s.cell)
    · rw [hi] at cond
      simp only [Bool.and_false, Bool.false_and] at cond
      cases cond
    · rw [hi] at cond
      simp only [Bool.and_false, Bool.false_and] at cond
      cases cond
    · rw [hc] at cond
      simp only [Bool.and_false, Bool.false_and] at cond
      cases cond
    · have hinc : s.incrementRef = s.cell := eq_of_beq hi
      have hcur : s.currentRef = s.cell := eq_of_beq hc
      rw [directSourceProgram, hi, hc]
      unfold normalize classify resolve
      rw [hinc, hcur]
      dsimp only [List.find?]
      rw [beq_self_eq_true]
      rfl
  · cases h

theorem source_program_sound (s : SourceAst) (p : Program)
    (h : sourceProgram s = some p) : directSourceProgram s = some p := by
  unfold sourceProgram at h
  cases hs : analyzeSource s with
  | none => rw [hs] at h; cases h
  | some g =>
    rw [hs] at h
    dsimp at h
    rw [source_frontend_general s g hs]
    exact h

theorem output_program_correct (s : SourceAst) (o : OutputAst) (p : Program)
    (h : outputProgram s o = some p) :
    directOutputProgram o = some p ∧
      (outputStartup o).bind readyState = some (initialState p) := by
  unfold outputProgram at h
  cases hg : analyzeOutput s o with
  | none => rw [hg] at h; cases h
  | some g =>
    rw [hg] at h
    dsimp at h
    cases hn : normalize g with
    | none => rw [hn] at h; cases h
    | some n =>
      rw [hn] at h
      dsimp at h
      cases hd : directOutputProgram o with
      | none => rw [hd] at h; cases h
      | some d =>
        rw [hd] at h
        dsimp at h
        cases hi : (outputStartup o).bind readyState with
        | none => rw [hi] at h; cases h
        | some initial =>
          rw [hi] at h
          dsimp at h
          split at h
          · rename_i eq
            split at h
            · rename_i initialEq
              have heq : n = d := eq_of_beq eq
              have hinit : initial = initialState d := eq_of_beq initialEq
              cases h
              constructor
              · rw [← heq]
              · rw [hinit, ← heq]
            · cases h
          · cases h

theorem output_frontend_general (s : SourceAst) (o : OutputAst) (p : Program)
    (h : outputProgram s o = some p) : directOutputProgram o = some p :=
  (output_program_correct s o p h).1

theorem output_startup_general (s : SourceAst) (o : OutputAst) (p : Program)
    (h : outputProgram s o = some p) :
    (outputStartup o).bind readyState = some (initialState p) :=
  (output_program_correct s o p h).2

theorem frontend_operational_general (s : SourceAst) (o : OutputAst) (p : Program)
    (sourceResolved : sourceProgram s = some p)
    (outputResolved : outputProgram s o = some p)
    (state : State) (call : Call) :
    sourceOperationalStep s state call = outputOperationalStep o state call := by
  rw [source_step_sound s p state call (source_program_sound s p sourceResolved)]
  rw [output_step_sound o p state call (output_frontend_general s o p outputResolved)]

theorem source_startup_general (s : SourceAst) (p : Program)
    (h : sourceProgram s = some p) :
    (sourceStartup s).bind readyState = some (initialState p) := by
  have direct := source_program_sound s p h
  unfold directSourceProgram at direct
  split at direct
  · cases direct
    rfl
  · cases direct

def runOperational (transition : State → Call → Option (State × Option Nat)) :
    State → List Call → Option (State × List Nat)
  | state, [] => some (state, [])
  | state, call :: calls => do
    let (next, observation) ← transition state call
    let (final, observations) ← runOperational transition next calls
    pure (final, match observation with | none => observations | some value => value :: observations)

theorem run_operational_congr
    (left right : State → Call → Option (State × Option Nat))
    (same : ∀ state call, left state call = right state call)
    (state : State) (calls : List Call) :
    runOperational left state calls = runOperational right state calls := by
  induction calls generalizing state with
  | nil => rfl
  | cons call calls ih =>
    rw [runOperational, runOperational, same]
    cases h : right state call with
    | none => rfl
    | some result =>
      cases result with
      | mk next observation =>
        dsimp
        rw [ih]

theorem frontend_operational_sequence_general (s : SourceAst) (o : OutputAst) (p : Program)
    (sourceResolved : sourceProgram s = some p)
    (outputResolved : outputProgram s o = some p)
    (state : State) (calls : List Call) :
    runOperational (sourceOperationalStep s) state calls =
      runOperational (outputOperationalStep o) state calls :=
  run_operational_congr _ _ (frontend_operational_general s o p sourceResolved outputResolved) state calls
