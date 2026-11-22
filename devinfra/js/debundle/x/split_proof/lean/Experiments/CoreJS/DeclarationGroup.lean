import Experiments.CoreJS.Eval

namespace CoreJS

def DeclKind.Initializer : DeclKind → Type
  | .var | .let_ => Option Expr
  | .const => Expr

structure Declarator (kind : DeclKind) where
  name : Name
  init : kind.Initializer

/-- A nonempty source declaration list. Named exports are linkage, separate from evaluation. -/
structure DeclarationGroup where
  kind : DeclKind
  first : Declarator kind
  rest : List (Declarator kind)
  exported : Bool := false

namespace Declarator

def statement : {kind : DeclKind} → Declarator kind → Stmt
  | .var, ⟨x, none⟩ => .varDecl x
  | .var, ⟨x, some e⟩ => .decl .var x e
  | .let_, ⟨x, e⟩ => .decl .let_ x (e.getD .undef)
  | .const, ⟨x, e⟩ => .decl .const x e

private def binding (kind : DeclKind) : Binding :=
  { value := if kind == .var then some .undef else none, mutable := kind != .const }

private theorem statement_bindings {kind : DeclKind} (d : Declarator kind) :
    d.statement.bindings = [(d.name, binding kind)] := by
  cases kind with
  | var => obtain ⟨x, e⟩ := d; cases e <;> rfl
  | let_ => rfl
  | const => rfl

private def eval {W : World} : Nat → Env → {kind : DeclKind} → Declarator kind → M W Flow
  | 0, _, _, _ => outOfFuel
  | _ + 1, _, .var, ⟨_, none⟩ => pure .normal
  | n + 1, env, .var, ⟨x, some e⟩ => do
    writeVar env x (← evalExpr n env e)
    pure .normal
  | n + 1, env, .let_, ⟨x, e⟩ => do
    initVar env x (← evalExpr n env (e.getD .undef))
    pure .normal
  | n + 1, env, .const, ⟨x, e⟩ => do
    initVar env x (← evalExpr n env e)
    pure .normal

private theorem eval_eq {W : World} (fuel : Nat) (env : Env) {kind : DeclKind} (d : Declarator kind) :
    (eval fuel env d : M W Flow) = execStmt fuel env d.statement := by
  cases fuel with
  | zero => rfl
  | succ fuel =>
    cases kind with
    | var => obtain ⟨x, e⟩ := d; cases e <;> rfl
    | let_ => cases d; rfl
    | const => cases d; rfl

private theorem initFunction_statement {W : World} (env : Env) (id : Nat) {kind : DeclKind} (d : Declarator kind) :
    (initFunction (id, d.statement, env) : M W Unit) = pure () := by
  cases kind with
  | var => obtain ⟨x, e⟩ := d; cases e <;> rfl
  | let_ => cases d; rfl
  | const => cases d; rfl

end Declarator

namespace DeclarationGroup

variable {W : World}

private def declarators (g : DeclarationGroup) : List (Declarator g.kind) := g.first :: g.rest

def exports (g : DeclarationGroup) : List (Name × Name) :=
  if g.exported then g.declarators.map fun d => (d.name, d.name) else []

def size (g : DeclarationGroup) : Nat := g.declarators.length

private def compileList {kind : DeclKind} : Nat → List (Declarator kind) → List (Nat × Stmt)
  | _, [] => []
  | id, d :: ds => (id, d.statement) :: compileList (id + 1) ds

def compile (g : DeclarationGroup) (start : Nat) : List (Nat × Stmt) := compileList start g.declarators

def compileGroups : Nat → List DeclarationGroup → List (Nat × Stmt)
  | _, [] => []
  | start, g :: gs => g.compile start ++ compileGroups (start + g.size) gs

private abbrev Frames := List (Nat × List (Name × Addr))

/-- Instantiate one cell in each declarator's region, even for a repeated `var` name.
The containing module's scope resolves repeated names to the first cell. -/
private def declareList {kind : DeclKind} (tail : M W Frames) : Nat → List (Declarator kind) → M W Frames
  | _, [] => tail
  | id, d :: ds => do
    let frame ← inRegion id (newCells [(d.name, Declarator.binding kind)])
    let rest ← declareList tail (id + 1) ds
    pure ((id, frame) :: rest)

def declareGroups : Nat → List DeclarationGroup → M W Frames
  | _, [] => pure []
  | start, g :: gs => declareList (declareGroups (start + g.size) gs) start g.declarators

/-- Evaluate initializers in declarator order. Each declarator uses its own model region
and the same initial fuel bound, and an abrupt completion skips every remaining declarator. -/
private def runList {kind : DeclKind} (fuel : Nat) (env : Env) (tail : M W Unit) :
    Nat → List (Declarator kind) → M W Unit
  | _, [] => tail
  | id, d :: ds => do
    let _ ← inRegion id (Declarator.eval fuel env d)
    runList fuel env tail (id + 1) ds

def runGroups (fuel : Nat) (env : Env) : Nat → List DeclarationGroup → M W Unit
  | _, [] => pure ()
  | start, g :: gs => runList fuel env (runGroups fuel env (start + g.size) gs) start g.declarators

private theorem compileList_length {kind : DeclKind} (start : Nat) (ds : List (Declarator kind)) :
    (compileList start ds).length = ds.length := by
  induction ds generalizing start with
  | nil => rfl
  | cons d ds ih => simp [compileList, ih]

theorem compileGroups_length (start : Nat) (gs : List DeclarationGroup) :
    (compileGroups start gs).length = (gs.map DeclarationGroup.size).sum := by
  induction gs generalizing start with
  | nil => rfl
  | cons g gs ih => simp [compileGroups, compile, size, compileList_length, ih]

private theorem compileList_ids {kind : DeclKind} (start : Nat) (ds : List (Declarator kind)) :
    (compileList start ds).map Prod.fst = List.range' start ds.length := by
  induction ds generalizing start with
  | nil => rfl
  | cons d ds ih => simp [compileList, ih, List.range'_succ]

theorem compile_ids (g : DeclarationGroup) (start : Nat) :
    (g.compile start).map Prod.fst = List.range' start g.size := compileList_ids start g.declarators

theorem compileGroups_ids (start : Nat) (gs : List DeclarationGroup) :
    (compileGroups start gs).map Prod.fst = List.range' start (compileGroups start gs).length := by
  induction gs generalizing start with
  | nil => rfl
  | cons g gs ih =>
    have hlen : (g.compile start).length = g.size := compileList_length start g.declarators
    rw [compileGroups, List.map_append, List.length_append, compile_ids, ih, hlen]
    simpa only [Nat.one_mul] using
      (@List.range'_append start g.size (compileGroups (start + g.size) gs).length 1)

private theorem declareList_append {kind : DeclKind} (start : Nat) (ds : List (Declarator kind))
    (tops : List (Nat × Stmt)) :
    (declareList (declareTops tops) start ds : M W Frames) = declareTops (compileList start ds ++ tops) := by
  induction ds generalizing start with
  | nil => rfl
  | cons d ds ih => simp only [declareList, compileList, List.cons_append, declareTops, Declarator.statement_bindings, ih]

/-- This is equality of computations, so it includes their states and failures during allocation. -/
theorem declareGroups_eq (start : Nat) (gs : List DeclarationGroup) :
    (declareGroups start gs : M W Frames) = declareTops (compileGroups start gs) := by
  induction gs generalizing start with
  | nil => rfl
  | cons g gs ih => simp only [declareGroups, compileGroups, compile, ih, declareList_append]

private theorem runList_append {kind : DeclKind} (fuel : Nat) (env : Env) (start : Nat) (ds : List (Declarator kind))
    (units : List (Nat × Stmt × Env)) :
    (runList fuel env (runUnits fuel units) start ds : M W Unit) =
      runUnits fuel (((compileList start ds).map fun (id, s) => (id, s, env)) ++ units) := by
  induction ds generalizing start with
  | nil => rfl
  | cons d ds ih => simp only [runList, compileList, List.map_cons, List.cons_append, runUnits, runUnit, Declarator.eval_eq, ih]

/-- Equality for every world, fuel, and state, including abrupt completion and its state. -/
theorem runGroups_eq (fuel : Nat) (env : Env) (start : Nat) (gs : List DeclarationGroup) :
    (runGroups fuel env start gs : M W Unit) =
      runUnits fuel ((compileGroups start gs).map fun (id, s) => (id, s, env)) := by
  induction gs generalizing start with
  | nil => rfl
  | cons g gs ih => simp only [runGroups, compileGroups, compile, ih, List.map_append, runList_append]

private theorem initFunctions_compileList {kind : DeclKind} (env : Env) (start : Nat) (ds : List (Declarator kind))
    (units : List (Nat × Stmt × Env)) :
    (initFunctions (((compileList start ds).map fun (id, s) => (id, s, env)) ++ units) : M W Unit) = initFunctions units := by
  induction ds generalizing start with
  | nil => rfl
  | cons d ds ih =>
    simp only [compileList, List.map_cons, List.cons_append, initFunctions, Declarator.initFunction_statement, ih]
    rfl

theorem initFunctions_compileGroups (env : Env) (start : Nat) (gs : List DeclarationGroup) :
    (initFunctions ((compileGroups start gs).map fun (id, s) => (id, s, env)) : M W Unit) = pure () := by
  induction gs generalizing start with
  | nil => rfl
  | cons g gs ih => simp only [compileGroups, compile, List.map_append, initFunctions_compileList, ih]

end DeclarationGroup
end CoreJS
