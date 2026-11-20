import Experiments.LocalCall

/-! A small effect model for a property read followed by a receiver write.

This is a separate model, not an ECMAScript semantics or an extension of MiniJS.
An accessor records a marker in a trace; it abstracts away arbitrary code, throwing,
proxies, descriptors and prototype chains. The classifier consumes concrete object
states: establishing and preserving those states is a separate obligation.
-/

namespace PropertyRead

inductive Slot where
  | data (value : Nat)
  | accessor (marker : Int)
  deriving DecidableEq, Repr

/-- One property on an object and the corresponding property on its prototype. -/
structure Object where
  own : Option Slot
  inherited : Option Slot
  deriving DecidableEq, Repr

abbrev Trace := MJS.World

/-- Read an own property first; otherwise consult the modeled prototype. -/
def read (o : Object) (trace : Trace) : Nat × Trace :=
  match o.own.orElse (fun _ => o.inherited) with
  | some (.data value) => (value, trace)
  | some (.accessor marker) => (0, trace ++ [marker])
  | none => (0, trace)

/-- Write to an own data property without consulting the prototype. Other writes
may invoke an own or inherited accessor in this model. -/
def write (o : Object) (value : Nat) (trace : Trace) : Object × Trace :=
  match o.own with
  | some (.data _) => ({ o with own := some (.data value) }, trace)
  | some (.accessor marker) => (o, trace ++ [marker])
  | none =>
    match o.inherited with
    | some (.accessor marker) => (o, trace ++ [marker])
    | _ => ({ o with own := some (.data value) }, trace)

/-- A known own data property protects the read from any prototype accessor. -/
def hasOwnData (o : Object) : Bool :=
  match o.own with
  | some (.data _) => true
  | _ => false

/-- A property with no accessor on the modeled lookup path can be read without
changing the trace. This is a fact about the supplied state, not an inference. -/
def readNoAccessor (o : Object) : Bool :=
  match o.own.orElse (fun _ => o.inherited) with
  | some (.accessor _) => false
  | _ => true

theorem read_no_accessor_preserves_trace (o : Object) (trace : Trace)
    (h : readNoAccessor o = true) : (read o trace).2 = trace := by
  cases o with
  | mk own inherited =>
    cases own with
    | none =>
      cases inherited with
      | none => rfl
      | some slot => cases slot <;> simp_all [readNoAccessor, read]
    | some slot => cases slot <;> simp_all [readNoAccessor, read]

theorem own_data_read_ignores_prototype (value : Nat) (proto proto' : Option Slot) (trace : Trace) :
    read ⟨some (.data value), proto⟩ trace = read ⟨some (.data value), proto'⟩ trace := by
  rfl

theorem own_data_write_preserves_trace (o : Object) (value : Nat) (trace : Trace)
    (h : hasOwnData o = true) : (write o value trace).2 = trace := by
  cases ho : o.own with
  | none => simp [hasOwnData, ho] at h
  | some slot => cases slot <;> simp_all [hasOwnData, write]

/-- Model the relevant part of a local constructor: read the argument and write
the result to a receiver property. The callee itself is fixed in this model. -/
def runInit (arg receiver : Object) (trace : Trace) : Object × Trace :=
  let (value, trace') := read arg trace
  write receiver value trace'

/-- Both facts must hold in the states actually used by the call. The proof says
nothing about whether an external call changed those states before the call. -/
theorem initialize_preserves_trace (arg receiver : Object) (trace : Trace)
    (hread : readNoAccessor arg = true) (hwrite : hasOwnData receiver = true) :
    (runInit arg receiver trace).2 = trace := by
  simp only [runInit]
  rw [own_data_write_preserves_trace receiver (read arg trace).1 (read arg trace).2 hwrite]
  exact read_no_accessor_preserves_trace arg trace hread

/-- Compose the prior MiniJS local-call theorem with the separate property model.
This assumes the same trace represents both worlds, that the local binding is the
stated closure, and that the supplied object states hold at the later read/write. -/
theorem local_call_then_initialize_preserves_trace
    (G : MJS.Env) (fn x : EsmSplit.Name) (body : MJS.Expr) (k : Int)
    (hfn : G fn = some (.lam x body)) (hbody : body.inert = true)
    (fuel : Nat) (trace trace' : Trace) (value : MJS.Expr)
    (heval : MJS.eval G fuel (.app (.gvar fn) (.num k)) trace = some (value, trace'))
    (arg receiver : Object) (hread : readNoAccessor arg = true) (hwrite : hasOwnData receiver = true) :
    (runInit arg receiver trace').2 = trace := by
  have h := MJS.local_call_world G fn x body k hfn hbody fuel trace
  rw [heval] at h
  cases hzero : MJS.eval G fuel (.app (.gvar fn) (.num k)) [] with
  | none => simp [hzero] at h
  | some pair =>
    cases pair with
    | mk v w =>
      simp only [hzero, Option.map_some, Option.some.injEq, Prod.mk.injEq] at h
      rw [initialize_preserves_trace arg receiver trace' hread hwrite]
      exact h.2

theorem own_data_positive :
    runInit ⟨some (.data 7), some (.accessor 11)⟩
      ⟨some (.data 0), some (.accessor 13)⟩ [] = (⟨some (.data 7), some (.accessor 13)⟩, []) := by
  decide

theorem missing_argument_invokes_getter :
    (runInit ⟨none, some (.accessor 11)⟩ ⟨some (.data 0), none⟩ []).2 = [11] := by
  decide

theorem fresh_receiver_invokes_setter :
    (runInit ⟨some (.data 7), none⟩ ⟨none, some (.accessor 13)⟩ []).2 = [13] := by
  decide

theorem inherited_getter_rejected : readNoAccessor ⟨none, some (.accessor 11)⟩ = false := by decide
theorem inherited_setter_rejected : hasOwnData ⟨none, some (.accessor 13)⟩ = false := by decide

/-- Without an own property, even two non-accessor prototype states can return
different values. The read's dependency on the inherited slot is not optional. -/
theorem missing_read_depends_on_prototype :
    (read ⟨none, some (.data 1)⟩ []).1 ≠ (read ⟨none, some (.data 2)⟩ []).1 := by decide

/-- A check on an earlier state cannot justify a call after the prototype changes. -/
theorem prototype_mutation_invalidates_guard :
    readNoAccessor ⟨none, none⟩ = true ∧
      readNoAccessor ⟨none, some (.accessor 11)⟩ = false ∧
      (read ⟨none, some (.accessor 11)⟩ []).2 = [11] := by decide

end PropertyRead
