import EsmSplit.CoreJS.LinkedBehavior

/-! Source linking for a lexical cell followed by two hoisted ordinary functions.
The lexical declaration is initialized by execution, not linking. -/

namespace CoreJS.LexicalFunctionSource

set_option maxRecDepth 16000
set_option maxHeartbeats 4000000

def writer (name param : Name) (body : List Stmt) : Func := .mk name .normal false [param] body
def reader (name : Name) (body : List Stmt) : Func := .mk name .normal false [] body

def sourceModule (cell writeName readName initial param : Name) (writeBody readBody : List Stmt) (init : Expr)
    (exports : List (Name × Name)) : Module :=
  ⟨"entry.mjs", [], [(0, .decl .let_ cell .undef),
    (1, .funDecl (writer writeName param writeBody)), (2, .funDecl (reader readName readBody)),
    (3, .decl .const initial init)], exports⟩

def source (cell writeName readName initial param : Name) (writeBody readBody : List Stmt) (init : Expr)
    (exports : List (Name × Name)) : Program :=
  ⟨"entry.mjs", [sourceModule cell writeName readName initial param writeBody readBody init exports]⟩

/-- Link the original source before running any statement, for every host world. -/
def sourceLink (w : World) (cell writeName readName initial param : Name)
    (writeBody readBody : List Stmt) (init : Expr) (exports : List (Name × Name))
    (cw : cell ≠ writeName) (cr : cell ≠ readName) (wr : writeName ≠ readName) :
    LinkedBehavior.CheckedLink (source cell writeName readName initial param writeBody readBody init exports) w := by
  have wc : writeName ≠ cell := Ne.symm cw
  have rc : readName ≠ cell := Ne.symm cr
  have rw : readName ≠ writeName := Ne.symm wr
  refine ⟨?_, ?_, ?_⟩
  case refine_3 =>
    cbv
    simp only [wc, decide_false]
    cbv
    simp only [rc, decide_false]
    cbv
    simp only [rw, decide_false]
    cbv
    rfl

/-- At link time the mutable lexical cell is still in the temporal dead zone. -/
theorem lexical_uninitialized (w : World) (cell writeName readName initial param : Name)
    (writeBody readBody : List Stmt) (init : Expr) (exports : List (Name × Name))
    (cw : cell ≠ writeName) (cr : cell ≠ readName) (wr : writeName ≠ readName) :
    (sourceLink w cell writeName readName initial param writeBody readBody init exports cw cr wr).ready.cellAt ⟨1, 0⟩ =
      some ⟨none, true⟩ := by
  rfl

/-- Running an initializer-free `let` writes `undefined` to its original
cell. The previous link-time state is not modified until this step executes. -/
theorem execute_let_undefined (w : World) (fuel : Nat) (env : Env) (name : Name)
    (addr : Addr) (s : State w) (lookup : env.vars.lookup name = some addr) :
    execStmt (fuel + 2) env (.decl .let_ name .undef) s =
      .ok .normal { s with cells := (s.cells.modify addr.region
        (fun bs => bs.modify addr.index (fun binding => { binding with value := some .undef }))) } := by
  simp only [execStmt, evalExpr, initVar, lookup]
  rfl

/-- A check against the original decoded program. Bodies, initializer and
export metadata come from the source; only the supported declaration shape is
accepted. Distinct bindings prevent one hoist from initializing another cell. -/
structure Checked (original : Program) where
  cell : Name
  writeName : Name
  readName : Name
  initial : Name
  param : Name
  writeBody : List Stmt
  readBody : List Stmt
  init : Expr
  exports : List (Name × Name)
  sourceEq : original = source cell writeName readName initial param writeBody readBody init exports
  cellWrite : cell ≠ writeName
  cellRead : cell ≠ readName
  writeRead : writeName ≠ readName

def check (original : Program) : Except String (Checked original) := do
  match shape : original with
  | ⟨entry, [⟨moduleName, [], [(0, .decl .let_ cell .undef),
      (1, .funDecl (.mk writeName .normal false [param] writeBody)),
      (2, .funDecl (.mk readName .normal false [] readBody)),
      (3, .decl .const initial init)], exports⟩]⟩ =>
    if names : entry = "entry.mjs" ∧ moduleName = "entry.mjs" ∧
        cell ≠ writeName ∧ cell ≠ readName ∧ writeName ≠ readName then
      have eq : (⟨entry, [⟨moduleName, [], [(0, .decl .let_ cell .undef),
          (1, .funDecl (.mk writeName .normal false [param] writeBody)),
          (2, .funDecl (.mk readName .normal false [] readBody)),
          (3, .decl .const initial init)], exports⟩]⟩ : Program) =
          source cell writeName readName initial param writeBody readBody init exports := by
        simp [source, sourceModule, writer, reader, names.1, names.2.1]
      return ⟨cell, writeName, readName, initial, param, writeBody, readBody, init, exports,
        shape.trans eq, names.2.2.1, names.2.2.2.1, names.2.2.2.2⟩
    else throw "source module path or declaration names are unsupported"
  | _ => throw "expected a lexical declaration and two ordinary function declarations"

/-- The link is derived from the checked source, rather than supplied by a caller. -/
def Checked.link {original : Program} (c : Checked original) (w : World) :
    LinkedBehavior.CheckedLink original w := by
  rw [c.sourceEq]
  exact sourceLink w c.cell c.writeName c.readName c.initial c.param c.writeBody c.readBody c.init c.exports
    c.cellWrite c.cellRead c.writeRead

/-- The checked original's lexical cell is uninitialized after its real link. -/
theorem Checked.lexical_uninitialized {original : Program} (c : Checked original) (w : World) :
    (c.link w).ready.cellAt ⟨1, 0⟩ = some ⟨none, true⟩ := by
  cases c with
  | mk cell writeName readName initial param writeBody readBody init exports sourceEq cw cr wr =>
    cases sourceEq
    exact CoreJS.LexicalFunctionSource.lexical_uninitialized w cell writeName readName initial param writeBody readBody init exports cw cr wr

#print axioms execute_let_undefined
#print axioms check
#print axioms Checked.link
#print axioms Checked.lexical_uninitialized
#print axioms sourceLink
#print axioms lexical_uninitialized

end CoreJS.LexicalFunctionSource
