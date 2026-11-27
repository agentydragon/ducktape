import EsmSplit.CoreJS.MetadataCell
import EsmSplit.CoreJS.LinkWorld

/-! A linker experiment for an already provisioned entry record. Two admissible
receiver initializers disagree about reachability at link time. -/

namespace CoreJS.MetadataLink

open MetadataCell LinkedBehavior

/-- The caller identifies the retained record and materializes its receiver.
Install its immutable cell before any hoisted closure captures the entry scope.
Other module scopes do not get this capability. This does not model a lazy or
side-effecting host initialization hook. -/
def linkRecord (p : Program) (record : String) (receiver : Env → M W Addr) : M W (List (Module × Env)) :=
  linkUsingScopes p fun frames => do
    unless p.entry == record do return ← unknown "metadata entry record changed"
    let scopes ← lift (p.scopes frames p.loaded)
    scopes.mapM fun (md, env) => do
      if md.name != record then return (md, env)
      let a ← receiver env
      let _ ← getObj a
      let c ← newCell ⟨some (.obj a), false⟩
      return (md, {env with vars := (slot, c) :: env.vars})

private def record := "main.mjs"
private def worker := "worker.mjs"
private def readUnit : Nat × Stmt := (0, .funDecl (.mk "read" .normal false [] [
  .ret (path "env" "MODE")]))
private def observe : Nat × Stmt := (1, .decl .var "observed" (.call (.var "read") []))
private def moveUnit : Nat × Stmt := (2, .decl .var "pivot" (.str "ready"))

private def graph (moved : Bool) : Program :=
  let entry : Module := {
    name := record
    body := [readUnit, observe] ++ (if moved then [] else [moveUnit])
    imports := if moved then [⟨worker, [("pivot", "pivot")]⟩] else []
    exports := [("observed", "observed"), ("pivot", "pivot")] }
  let other : Module := {
    name := worker
    body := [moveUnit]
    imports := []
    exports := [("pivot", "pivot")] }
  {entry := record, modules := if moved then [entry, other] else [entry]}

private def world (live : Bool) : World where
  σ := Bool
  init := live
  step := fun _ _ _ s => (.ret .undef, s)
  talk := fun _ s => (.unknown "unexpected opaque call", s)

/-- The two hosts allocate the same addresses. One supplies a data property,
the other a getter returning the entry's live pivot binding. -/
private def receiver (env : Env) : M (world live) Addr := do
  let getter : Obj := {proto := none, props := [], code := some (.closure
    (.mk "MODE" .method false [] [.ret (.var "pivot")]) (env.capture ["pivot"]) [])}
  let plain : Obj := {proto := none, props := []}
  let hasGetter : Bool := (← getState).world.state
  let g ← alloc (if hasGetter then getter else plain)
  let inner ← alloc {proto := none, props := [("MODE", if hasGetter then
      .accessor (.obj g) .undef true true else .data .undef true true true)]}
  alloc {proto := none, props := [("env", .data (.obj inner) true true true)]}

private def programResult (live moved : Bool) : Res (world live) Val :=
  let p := graph moved
  (do
    let linked ← linkRecord p record (receiver (live := live))
    let some (_, entry) := linked.find? (fun (pair : Module × Env) => pair.1.name == record) | unknown "missing entry"
    runUnits 24 (unitsOf linked)
    readVar entry "observed") (initialState (world live) p.regions)

private def observed (live moved : Bool) : Option Val :=
  match programResult live moved with | .ok v _ => some v | _ => none

-- Identical entry URL, exports and retained reader. Moving the initializer
-- is undetectable in the data world but observable through the getter.
#guard observed false false == some .undef && observed false true == some .undef
#guard observed true false == some .undef && observed true true == some (.str "ready")

private def awayPivot (node : HeapNode) := node.addr.region != moveUnit.1 + 1

private def separated (w : World) (s : State w) : Bool := accessOk s [] awayPivot

private def linkAccess (live : Bool) : Option Bool :=
  let p := graph false
  match linkRecord p record (receiver (live := live)) (initialState (world live) p.regions) with
  | .ok _ s => some (separated _ s)
  | _ => none

theorem access_differs : linkAccess false = some true ∧ linkAccess true = some false := by
  constructor <;> cbv

/-- Even agreement on the single Boolean used for pivot isolation is impossible
with one transported link snapshot. The production transport copies the heap
and cells and changes only world state, which accessOk does not consult. -/
theorem no_common_probe : ¬ ∃ s : State World.probe, ∀ live,
    linkAccess live = some (separated _ (linkState (world live) s)) := by
  rintro ⟨s, h⟩
  have equal : linkAccess false = linkAccess true := (h false).trans (h true).symm
  rw [access_differs.1, access_differs.2] at equal
  contradiction

end CoreJS.MetadataLink
