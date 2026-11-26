import EsmSplit.CoreJS.LinkedBehavior

/-! The linker constructs scopes and allocates hoisted functions. Its result
can be checked in one concrete world and transported to every initial host
state. No host step is used during linking. -/

namespace CoreJS.LinkedBehavior

/-- Instantiating a link snapshot changes its world state to the new world's
initial state. The handed objects and all ordinary CoreJS fields are retained. -/
def linkState (w : World) (s : State World.probe) : State w where
  heap := s.heap
  cells := s.cells
  region := s.region
  out := s.out
  world := { state := w.init, handed := s.world.handed }

private def transportResult {α : Type} (w : World) : Res World.probe α → Res w α
  | .ok a s => .ok a (linkState w s)
  | .throw v s => .throw v (linkState w s)
  | .unknown why s => .unknown why (linkState w s)
  | .fuel => .fuel

private def WorldIndependent {α : Type} (w : World) (concrete : M World.probe α) (general : M w α) : Prop :=
  ∀ s : State World.probe, general (linkState w s) = transportResult w (concrete s)

private theorem wi_pure {α : Type} (w : World) (a : α) :
    WorldIndependent w (pure a) (pure a) := fun _ => rfl

private theorem WorldIndependent.bind {w : World} {α β : Type} {m : M World.probe α} {n : M w α}
    {f : α → M World.probe β} {g : α → M w β}
    (ha : WorldIndependent w m n) (hf : ∀ a, WorldIndependent w (f a) (g a)) :
    WorldIndependent w (m >>= f) (n >>= g) := by
  intro s
  simp only [M.bind_apply, ha s]
  cases m s with
  | ok a t => exact hf a t
  | _ => rfl

private theorem wi_lift {α : Type} (w : World) (a : Except Abort α) :
    WorldIndependent w (lift a) (lift a) := by
  intro s
  cases a with
  | ok => rfl
  | error reason => cases reason <;> rfl

private theorem wi_region {w : World} {α : Type} {m : M World.probe α} {n : M w α}
    (id : Nat) (h : WorldIndependent w m n) :
    WorldIndependent w (inRegion id m) (inRegion id n) := by
  unfold inRegion
  apply WorldIndependent.bind (fun _ => rfl)
  intro _
  apply h.bind
  intro a
  exact WorldIndependent.bind (fun _ => rfl) fun _ => wi_pure _ a

private theorem wi_newCell (w : World) (b : Binding) :
    WorldIndependent w (newCell b) (newCell b) := by
  intro s
  simp only [newCell, linkState]
  split <;> rfl

private theorem wi_newCells (w : World) (bs : List (Name × Binding)) :
    WorldIndependent w (newCells bs) (newCells bs) := by
  induction bs with
  | nil => exact wi_pure _ _
  | cons b bs ih =>
    obtain ⟨name, binding⟩ := b
    exact (wi_newCell w binding).bind fun a => ih.bind fun frame => wi_pure w ((name, a) :: frame)

private theorem wi_alloc (w : World) (o : Obj) :
    WorldIndependent w (alloc o) (alloc o) := by
  intro s
  simp only [alloc, linkState]
  split <;> rfl

private theorem wi_addPrototype (w : World) (a : Addr) (kind : FuncKind) :
    WorldIndependent w (addPrototype a kind) (addPrototype a kind) := by
  unfold addPrototype
  exact (wi_alloc w _).bind fun _ _ => rfl

private theorem wi_initVar (w : World) (env : Env) (name : Name) (v : Val) :
    WorldIndependent w (initVar env name v) (initVar env name v) := by
  unfold initVar
  split <;> (intro s; rfl)

private theorem wi_makeFunc (w : World) (env : Env) (f : Func) :
    WorldIndependent w (makeFunc env f) (makeFunc env f) := by
  unfold makeFunc
  have scope : WorldIndependent w (selfScope env f.self f.name) (selfScope env f.self f.name) := by
    unfold selfScope
    split
    · unfold declare
      exact (wi_newCells w _).bind fun _ => wi_pure _ _
    · exact wi_pure _ _
  apply scope.bind
  intro closed
  apply (wi_alloc w _).bind
  intro a
  have prototype : WorldIndependent w
      (if f.kind.hasOwnPrototype then addPrototype a f.kind else pure ())
      (if f.kind.hasOwnPrototype then addPrototype a f.kind else pure ()) := by
    split
    · exact wi_addPrototype _ _ _
    · exact wi_pure _ _
  apply prototype.bind
  intro _
  have init : WorldIndependent w (initSelf closed f.self f.name (.obj a)) (initSelf closed f.self f.name (.obj a)) := by
    unfold initSelf
    split
    · exact wi_initVar _ _ _ _
    · exact wi_pure _ _
  exact init.bind fun _ => wi_pure _ _

private theorem wi_makeOpaque (w : World) (code part : Nat) (cells : List (Option Addr)) (name : String)
    (length : Nat) (constructor : Bool) (proto : Addr) :
    WorldIndependent w (makeOpaque code part cells name length constructor proto)
      (makeOpaque code part cells name length constructor proto) := by
  unfold makeOpaque
  apply (wi_alloc w _).bind
  intro a
  have prototype : WorldIndependent w (if constructor then addPrototype a else pure ())
      (if constructor then addPrototype a else pure ()) := by
    split
    · exact wi_addPrototype _ _ _
    · exact wi_pure _ _
  exact prototype.bind fun _ => wi_pure _ _

private theorem wi_initOpaque (w : World) (code : Nat) (cells : List (Option Addr)) (env : Env)
    (funs : List OpaqueHoist) (part : Nat) :
    WorldIndependent w (initOpaque code cells env funs part) (initOpaque code cells env funs part) := by
  induction funs generalizing part with
  | nil => exact wi_pure _ _
  | cons fn rest ih =>
    obtain ⟨name, length, kind⟩ := fn
    cases kind with
    | ordinary constructor =>
      exact (wi_makeOpaque w code part cells name length constructor functionProto).bind fun value =>
        (wi_initVar w env name value).bind fun _ => ih (part + 1)
    | asyncFn =>
      exact (wi_makeOpaque w code part cells name length false asyncFunctionProto).bind fun value =>
        (wi_initVar w env name value).bind fun _ => ih (part + 1)

private theorem wi_initFunctions (w : World) (units : List LinkedUnit) :
    WorldIndependent w (initFunctions units) (initFunctions units) := by
  induction units with
  | nil => exact wi_pure _ _
  | cons u us ih =>
    have fn : WorldIndependent w (initFunction u) (initFunction u) := by
      obtain ⟨id, stmt, env⟩ := u
      cases stmt <;> try exact wi_pure _ _
      case funDecl f =>
        exact wi_region id ((wi_makeFunc w env f).bind fun v => wi_initVar w env f.name v)
      case «opaque» code decls funs free run =>
        exact wi_region id (wi_initOpaque w code (opaqueCells env decls funs free) env funs 1)
    exact fn.bind fun _ => ih

private theorem wi_declareTops (w : World) (units : List (Nat × Stmt)) :
    WorldIndependent w (declareTops units) (declareTops units) := by
  induction units with
  | nil => exact wi_pure _ _
  | cons u us ih =>
    obtain ⟨id, stmt⟩ := u
    exact (wi_region id (wi_newCells w stmt.bindings)).bind fun frame =>
      ih.bind fun frames => wi_pure w ((id, frame) :: frames)

private theorem wi_link (w : World) (p : Program) :
    WorldIndependent w (link p) (link p) := by
  unfold link linkUsingScopes
  have ids : WorldIndependent w (checkIdentities p.tops) (checkIdentities p.tops) := by
    unfold checkIdentities
    split <;> (intro s; rfl)
  exact ids.bind fun _ => (wi_declareTops w p.tops).bind fun frames =>
    (wi_lift w (p.scopes frames p.loaded)).bind fun linked =>
      (wi_initFunctions w ((unitsOf linked).mergeSort fun a b => decide (a.1 ≤ b.1))).bind fun _ => wi_pure w linked

/-- A computed successful link in the probe world describes the scopes and
initial CoreJS state in every world. -/
theorem link_from_probe {p : Program} (concrete : CheckedLink p World.probe) (w : World) :
    link p (initialState w p.regions) = .ok concrete.linked (linkState w concrete.ready) := by
  change link p (linkState w (initialState World.probe p.regions)) = _
  rw [wi_link w p _, concrete.equation]
  rfl

def CheckedLink.forWorld {p : Program} (checked : CheckedLink p World.probe) (w : World) :
    CheckedLink p w :=
  ⟨checked.linked, linkState w checked.ready, link_from_probe checked w⟩

def checkLink (p : Program) : Except String (CheckedLink p World.probe) :=
  match h : link p (initialState World.probe p.regions) with
  | .ok linked ready => .ok ⟨linked, ready, h⟩
  | .unknown reason _ => .error reason
  | .throw _ _ => .error "the module graph threw during linking"
  | .fuel => .error "linking exhausted fuel"

/-- Checked links for two programs in the same probe world. -/
structure CheckedProbePair (source candidate : Program) where
  before : CheckedLink source World.probe
  after : CheckedLink candidate World.probe

def checkProbePair (source candidate : Program) : Except String (CheckedProbePair source candidate) := do
  let before ← (checkLink source).mapError ("original linker: " ++ ·)
  let after ← (checkLink candidate).mapError ("candidate linker: " ++ ·)
  return ⟨before, after⟩

theorem CheckedProbePair.sameExports {source candidate : Program} (pair : CheckedProbePair source candidate)
    (exportsMatched : exportsMatchFrom pair.before pair.after = true) : SameExports source candidate := by
  apply sameExports_of_match
  intro w
  rw [exportsMatch_from_checked (pair.before.forWorld w) (pair.after.forWorld w)]
  exact exportsMatched

#print axioms CheckedProbePair.sameExports

end CoreJS.LinkedBehavior
