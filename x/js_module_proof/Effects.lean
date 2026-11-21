/- A restricted synchronous, acyclic module initialization model. -/

structure EffectModule where
  imports : List Nat
  expectedImports : List Nat
  assignment : List Nat
  effects : List String
  deriving DecidableEq, Repr

def elementAt {α : Type} : List α → Nat → Option α
  | [], _ => none
  | value :: _, 0 => some value
  | _ :: rest, index + 1 => elementAt rest index

def assignmentsMatch (source : List String) (modules : List EffectModule) : Bool :=
  let assigned := modules.flatMap (fun m => m.assignment)
  modules.all (fun m => m.imports == m.expectedImports &&
      m.assignment.mapM (elementAt source) == some m.effects) &&
    assigned.all (fun i => i < source.length) &&
    (List.range source.length).all (fun i => assigned.count i == 1)

/- The module scheduler: entering a module schedules its
   dependencies followed by a finish action; finishing emits its effects. -/
inductive InitTask where
  | enter (index : Nat)
  | finish (index : Nat)
  deriving DecidableEq, Repr

structure Machine where
  pending : List InitTask
  active : List Nat
  done : List Nat
  events : List String
  deriving DecidableEq, Repr

def machineStep (modules : List EffectModule) (m : Machine) : Option Machine := do
  match m.pending with
  | [] => none
  | .enter index :: rest =>
    if m.done.contains index then some { m with pending := rest }
    else if m.active.contains index then none
    else
      let module ← elementAt modules index
      some { m with pending := module.imports.map InitTask.enter ++ [.finish index] ++ rest, active := index :: m.active }
  | .finish index :: rest =>
    match m.active with
    | top :: active =>
      if top == index then
        let module ← elementAt modules index
        some { pending := rest, active, done := index :: m.done, events := m.events ++ module.effects }
      else none
    | [] => none

def machineRun (modules : List EffectModule) : Nat → Machine → Option Machine
  | 0, m => if m.pending.isEmpty then some m else none
  | fuel + 1, m =>
    if m.pending.isEmpty then some m
    else do
      let next ← machineStep modules m
      machineRun modules fuel next

def machineTrace (modules : List EffectModule) (entry : Nat) : Option (List String) := do
  let maxSteps := 2 + modules.length + (modules.flatMap (fun m => m.imports)).length
  let final ← machineRun modules maxSteps ⟨[.enter entry], [], [], []⟩
  if final.done.length == modules.length && final.active.isEmpty then some final.events else none

def effectTrace (modules : List EffectModule) (entry : Nat) : Option (List String) :=
  machineTrace modules entry

theorem checked_trace_sound (modules : List EffectModule) (entry : Nat) (events : List String)
    (h : effectTrace modules entry = some events) : machineTrace modules entry = some events := by
  exact h

/- Replies are supplied by the host in call order. `some error` throws; an
   exhausted reply list means subsequent calls return normally. -/
def runEffects {α : Type} (events : List String) (replies : List (Option α)) : List String × Option α :=
  match runExecution ⟨[], [], replies, none, [], none, []⟩ (events.map Operation.recordText) with
  | some state => (state.events.filterMap (fun event => match event with | .text value => some value | .number _ => none), state.error)
  | none => ([], none)

def outputBehavior {α : Type} (modules : List EffectModule) (entry : Nat)
    (replies : List (Option α)) : Option (List String × Option α) :=
  (effectTrace modules entry).map (fun events => runEffects events replies)

theorem trace_preserves_behavior {α : Type} (source : List String)
    (modules : List EffectModule) (entry : Nat)
    (same : effectTrace modules entry = some source) (replies : List (Option α)) :
    outputBehavior modules entry replies = some (runEffects source replies) := by
  unfold outputBehavior
  rw [same]
  rfl
