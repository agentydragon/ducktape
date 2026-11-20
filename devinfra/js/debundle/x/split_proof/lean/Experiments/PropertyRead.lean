import Experiments.LocalCall

/-! A small property-effect model, not ECMAScript or an extension of MiniJS.
One own slot and one inherited slot are modeled. Accessors append a marker and
can either complete or throw; data descriptors carry writability. Undefined
reads are represented as zero. Proxies, arbitrary getter/setter bodies, multiple
prototype levels and allocation failures are outside this model.
-/

namespace PropertyRead

inductive Accessor where
  | returns (marker : Int)
  | throws (marker : Int)
  deriving DecidableEq, Repr

inductive Slot where
  | data (value : Nat) (writable : Bool)
  | accessor (getter setter : Option Accessor)
  deriving DecidableEq, Repr

structure Object where
  own : Option Slot
  inherited : Option Slot
  deriving DecidableEq, Repr

abbrev Trace := MJS.World

/-- A thrown completion keeps the observable events that occurred before it. -/
inductive Completion (α : Type) where
  | normal (value : α) (trace : Trace)
  | thrown (trace : Trace)
  deriving DecidableEq, Repr

private def invoke {α : Type} (a : Accessor) (value : α) (trace : Trace) : Completion α :=
  match a with
  | .returns marker => .normal value (trace ++ [marker])
  | .throws marker => .thrown (trace ++ [marker])

/-- A missing property or getter returns the model's zero placeholder. -/
def read (o : Object) (trace : Trace) : Completion Nat :=
  match o.own.orElse (fun _ => o.inherited) with
  | some (.data value _) => .normal value trace
  | some (.accessor (some getter) _) => invoke getter 0 trace
  | _ => .normal 0 trace

/-- Strict write to one slot of an extensible ordinary receiver. Missing setters
and non-writable data properties throw. Successful inherited writable-data writes
create an own writable property, not a write to the prototype. -/
def write (o : Object) (value : Nat) (trace : Trace) : Completion Object :=
  match o.own with
  | some (.data _ true) => .normal { o with own := some (.data value true) } trace
  | some (.data _ false) => .thrown trace
  | some (.accessor _ (some setter)) => invoke setter o trace
  | some (.accessor _ none) => .thrown trace
  | none =>
    match o.inherited with
    | some (.data _ false) => .thrown trace
    | some (.accessor _ (some setter)) => invoke setter o trace
    | some (.accessor _ none) => .thrown trace
    | _ => .normal { o with own := some (.data value true) } trace

/-- This guard requires the actual state at the write point, not just freshness. -/
def hasOwnWritableData (o : Object) : Bool :=
  match o.own with
  | some (.data _ true) => true
  | _ => false

/-- Conservatively reject any modeled accessor on the read path. -/
def readNoAccessor (o : Object) : Bool :=
  match o.own.orElse (fun _ => o.inherited) with
  | some (.accessor _ _) => false
  | _ => true

theorem read_no_accessor_preserves_trace (o : Object) (trace : Trace)
    (h : readNoAccessor o = true) : ∃ v, read o trace = .normal v trace := by
  cases o with
  | mk own inherited =>
    cases own with
    | none =>
      cases inherited with
      | none => exact ⟨0, rfl⟩
      | some slot => cases slot <;> simp_all [readNoAccessor, read]
    | some slot => cases slot <;> simp_all [readNoAccessor, read]

theorem own_data_read_ignores_prototype (value : Nat) (writable : Bool)
    (proto proto' : Option Slot) (trace : Trace) :
    read ⟨some (.data value writable), proto⟩ trace = read ⟨some (.data value writable), proto'⟩ trace := by
  rfl

theorem own_data_write_preserves_trace (o : Object) (value : Nat) (trace : Trace)
    (h : hasOwnWritableData o = true) :
    ∃ result, write o value trace = .normal result trace := by
  cases ho : o.own with
  | none => simp [hasOwnWritableData, ho] at h
  | some slot => cases slot with
    | data old writable => cases writable <;> simp_all [hasOwnWritableData, write]
    | accessor getter setter => simp [hasOwnWritableData, ho] at h

/-- Read the argument, and only if it completed, write to the receiver. -/
def runInit (arg receiver : Object) (trace : Trace) : Completion Object :=
  match read arg trace with
  | .thrown t => .thrown t
  | .normal value t => write receiver value t

/-- No observable events and normal completion, given actual state guards.
The result object is allowed to change; these guards are not inferred from JS. -/
theorem initialize_preserves_trace (arg receiver : Object) (trace : Trace)
    (hread : readNoAccessor arg = true) (hwrite : hasOwnWritableData receiver = true) :
    ∃ result, runInit arg receiver trace = .normal result trace := by
  obtain ⟨value, hv⟩ := read_no_accessor_preserves_trace arg trace hread
  obtain ⟨result, hr⟩ := own_data_write_preserves_trace receiver value trace hwrite
  exact ⟨result, by simp [runInit, hv, hr]⟩

/-- Separate MiniJS and property executions compose only under supplied state facts. -/
theorem local_call_then_initialize_preserves_trace
    (G : MJS.Env) (fn x : EsmSplit.Name) (body : MJS.Expr) (k : Int)
    (hfn : G fn = some (.lam x body)) (hbody : body.inert = true)
    (fuel : Nat) (trace trace' : Trace) (value : MJS.Expr)
    (heval : MJS.eval G fuel (.app (.gvar fn) (.num k)) trace = some (value, trace'))
    (arg receiver : Object) (hread : readNoAccessor arg = true) (hwrite : hasOwnWritableData receiver = true) :
    ∃ result, runInit arg receiver trace' = .normal result trace := by
  have h := MJS.local_call_world G fn x body k hfn hbody fuel trace
  rw [heval] at h
  cases hzero : MJS.eval G fuel (.app (.gvar fn) (.num k)) [] with
  | none => simp [hzero] at h
  | some pair =>
    cases pair with
    | mk v w =>
      simp only [hzero, Option.map_some, Option.some.injEq, Prod.mk.injEq] at h
      rw [h.2]
      exact initialize_preserves_trace arg receiver trace hread hwrite

theorem own_data_positive :
    runInit ⟨some (.data 7 false), some (.accessor (some (.returns 11)) none)⟩
      ⟨some (.data 0 true), some (.accessor none (some (.returns 13)))⟩ [] =
        .normal ⟨some (.data 7 true), some (.accessor none (some (.returns 13)))⟩ [] := by decide

theorem missing_argument_invokes_getter :
    runInit ⟨none, some (.accessor (some (.returns 11)) none)⟩ ⟨some (.data 0 true), none⟩ [] =
      .normal ⟨some (.data 0 true), none⟩ [11] := by decide

theorem fresh_receiver_invokes_setter :
    runInit ⟨some (.data 7 true), none⟩ ⟨none, some (.accessor none (some (.returns 13)))⟩ [] =
      .normal ⟨none, some (.accessor none (some (.returns 13)))⟩ [13] := by decide

theorem inherited_getter_rejected : readNoAccessor ⟨none, some (.accessor (some (.returns 11)) none)⟩ = false := by decide
theorem inherited_setter_rejected : hasOwnWritableData ⟨none, some (.accessor none (some (.returns 13)))⟩ = false := by decide

theorem missing_read_depends_on_prototype :
    read ⟨none, some (.data 1 true)⟩ [] ≠ read ⟨none, some (.data 2 true)⟩ [] := by decide

theorem prototype_mutation_invalidates_guard :
    readNoAccessor ⟨none, none⟩ = true ∧
      readNoAccessor ⟨none, some (.accessor (some (.returns 11)) none)⟩ = false ∧
      read ⟨none, some (.accessor (some (.returns 11)) none)⟩ [] = .normal 0 [11] := by decide

theorem own_readonly_throws : write ⟨some (.data 0 false), none⟩ 1 [] = .thrown [] := by decide
theorem inherited_readonly_throws : write ⟨none, some (.data 0 false)⟩ 1 [] = .thrown [] := by decide
theorem inherited_writable_creates_own :
    write ⟨none, some (.data 0 true)⟩ 1 [] = .normal ⟨some (.data 1 true), some (.data 0 true)⟩ [] := by decide
theorem throwing_getter_stops_write :
    runInit ⟨none, some (.accessor (some (.throws 11)) none)⟩ ⟨some (.data 0 true), none⟩ [] =
      .thrown [11] := by decide
theorem throwing_setter_preserves_prior_trace :
    runInit ⟨some (.data 1 true), none⟩ ⟨none, some (.accessor none (some (.throws 13)))⟩ [7] =
      .thrown [7, 13] := by decide
theorem missing_setter_throws :
    write ⟨none, some (.accessor none none)⟩ 1 [] = .thrown [] := by decide

end PropertyRead
