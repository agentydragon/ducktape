import EsmSplit.CoreJS.BindingMoves
import EsmSplit.CoreJS.LinkedBehavior
import EsmSplit.Split

/-! Exact commutation for declarations with literal initializers. -/

namespace CoreJS.ExactLiteralMove

inductive Literal where
  | undef | null | bool (value : Bool) | num (value : Float) | str (value : String)

def Literal.expr : Literal → Expr
  | .undef => .undef
  | .null => .null
  | .bool b => .bool b
  | .num n => .num n
  | .str s => .str s

def Literal.value : Literal → Val
  | .undef => .undef
  | .null => .null
  | .bool b => .bool b
  | .num n => .num n
  | .str s => .str s

def decl (name : Name) (value : Literal) : Stmt := .decl .const name value.expr

/-- Literal initializers capture no names, inspect no state, and make no world request. -/
theorem literal_closed (value : Literal) : value.expr.free = [] := by
  cases value <;> rfl

/-- A literal left operand determines the branch without evaluating or
rewriting the right operand. The selected branch retains the evaluator's
remaining fuel. This is an equality of computations, including every state
and every modeled outcome. -/
theorem eval_logical_literal {W : World} (n : Nat) (env : Env) (op : LogOp)
    (left : Literal) (right : Expr) :
    evalExpr (W := W) (n + 2) env (.logical op left.expr right) =
      (do
        if ← lift (shortCircuits op left.value) then pure left.value
        else evalExpr (n + 1) env right) := by
  cases left <;> simp only [Literal.expr, Literal.value, evalExpr, M.pure_bind]

/-- When the literal short-circuits, the original right-hand syntax cannot
read a binding, throw, allocate or make a world request. -/
theorem eval_logical_literal_skipped {W : World} (n : Nat) (env : Env) (op : LogOp)
    (left : Literal) (right : Expr) (skips : shortCircuits op left.value = .ok true) :
    evalExpr (W := W) (n + 2) env (.logical op left.expr right) = pure left.value := by
  rw [eval_logical_literal, skips]
  simp only [lift, M.pure_bind, ↓reduceIte]

/-- When the literal selects the right operand, its effects, failures and
fuel use are exactly those of evaluating that operand at the remaining fuel. -/
theorem eval_logical_literal_selected {W : World} (n : Nat) (env : Env) (op : LogOp)
    (left : Literal) (right : Expr) (selects : shortCircuits op left.value = .ok false) :
    evalExpr (W := W) (n + 2) env (.logical op left.expr right) = evalExpr (n + 1) env right := by
  rw [eval_logical_literal, selects]
  simp only [lift, M.pure_bind, Bool.false_eq_true, ↓reduceIte]

/-- At insufficient fuel neither operand produces an outcome. -/
theorem eval_logical_literal_low_fuel {W : World} (env : Env) (op : LogOp)
    (left : Literal) (right : Expr) :
    evalExpr (W := W) 0 env (.logical op left.expr right) = outOfFuel ∧
      evalExpr (W := W) 1 env (.logical op left.expr right) = outOfFuel := by
  constructor <;> rfl

/-- A short-circuited logical expression has no binding reads or writes in
its successful executions. The exact equations above cover other outcomes. -/
theorem logical_skipped_footprint {W : World} (fuel : Nat) (env : Env) (op : LogOp)
    (left : Literal) (right : Expr) (skips : shortCircuits op left.value = .ok true) :
    CellFootprint (fun _ => False) (fun _ => False)
      (evalExpr (W := W) fuel env (.logical op left.expr right)) := by
  cases fuel with
  | zero => exact cellFootprint_outOfFuel
  | succ n =>
    cases n with
    | zero =>
      rw [(eval_logical_literal_low_fuel (W := W) env op left right).2]
      exact cellFootprint_outOfFuel
    | succ n =>
      rw [eval_logical_literal_skipped n env op left right skips]
      exact cellFootprint_pure left.value

/-- A skipped operand does not require its free names to resolve during this
expression's evaluation, even when the two environments differ. -/
theorem logical_skipped_env_independent {W : World} (fuel : Nat) (first second : Env)
    (op : LogOp) (left : Literal) (right : Expr)
    (skips : shortCircuits op left.value = .ok true) :
    evalExpr (W := W) fuel first (.logical op left.expr right) =
      evalExpr fuel second (.logical op left.expr right) := by
  cases fuel with
  | zero => rfl
  | succ n =>
    cases n with
    | zero => rfl
    | succ n =>
      rw [eval_logical_literal_skipped n first op left right skips,
        eval_logical_literal_skipped n second op left right skips]

/-- Once linked to its own cell, this unit always initializes it without host effects.
The evaluator uses two fuel levels for the declaration and literal. -/
theorem run_literal {W : World} (n id : Nat) (env : Env) (name : Name) (value : Literal)
    (cell : Addr) (linked : env.vars.lookup name = some cell) (s : State W) :
    runUnit (n + 2) env id (decl name value) s =
      .ok .normal { s.assign cell value.value with region := 0 } := by
  cases value <;> simp [runUnit, inRegion, decl, Literal.expr, Literal.value, execStmt,
    evalExpr, initVar, linked, State.assign, modifyState, M.bind_apply, pure] <;> rfl

/-- A declaration whose literal left operand short-circuits has the same
cell effect as that literal, without evaluating its original right operand. -/
theorem run_logical_skipped {W : World} (n id : Nat) (env : Env) (name : Name)
    (op : LogOp) (left : Literal) (right : Expr) (cell : Addr)
    (skips : shortCircuits op left.value = .ok true)
    (linked : env.vars.lookup name = some cell) (s : State W) :
    runUnit (n + 3) env id (.decl .const name (.logical op left.expr right)) s =
      .ok .normal { s.assign cell left.value with region := 0 } := by
  simp [runUnit, inRegion, execStmt, eval_logical_literal_skipped n env op left right skips,
    initVar, linked, State.assign, modifyState, M.bind_apply, pure] <;> rfl

/-- Two linked literal declarations with distinct cells commute in any state.
The tail is arbitrary, including throws and host effects. -/
theorem swap_literals {W : World} (fuel i j : Nat) (env1 env2 : Env)
    (name1 name2 : Name) (v1 v2 : Literal) (c1 c2 : Addr)
    (h1 : env1.vars.lookup name1 = some c1) (h2 : env2.vars.lookup name2 = some c2)
    (distinct : c1 ≠ c2) (tail : List LinkedUnit) (s : State W) :
    runUnits fuel ((i, decl name1 v1, env1) :: (j, decl name2 v2, env2) :: tail) s =
      runUnits fuel ((j, decl name2 v2, env2) :: (i, decl name1 v1, env1) :: tail) s := by
  cases fuel with
  | zero => rfl
  | succ fuel =>
    cases fuel with
    | zero => cases v1 <;> cases v2 <;> rfl
    | succ n =>
      simp only [runUnits, M.bind_apply, run_literal n i env1 name1 v1 c1 h1,
        run_literal n j env2 name2 v2 c2 h2]
      have same :
          { ({ s.assign c1 v1.value with region := 0 }).assign c2 v2.value with region := 0 } =
            { ({ s.assign c2 v2.value with region := 0 }).assign c1 v1.value with region := 0 } := by
        simpa only [State.assign] using congrArg (fun t : State W => { t with region := 0 })
          (State.assign_comm s c1 c2 v1.value v2.value distinct)
      rw [same]

/-- A skipped logical initializer commutes with an independent literal
declaration. The RHS remains the original syntax, and the equality includes
fuel exhaustion as well as every outcome of the unchanged suffix. -/
theorem swap_skipped_literal {W : World} (fuel i j : Nat) (env1 env2 : Env)
    (name1 name2 : Name) (op : LogOp) (left : Literal) (right : Expr) (value : Literal)
    (c1 c2 : Addr) (skips : shortCircuits op left.value = .ok true)
    (h1 : env1.vars.lookup name1 = some c1) (h2 : env2.vars.lookup name2 = some c2)
    (distinct : c1 ≠ c2) (tail : List LinkedUnit) (s : State W) :
    runUnits fuel ((i, .decl .const name1 (.logical op left.expr right), env1) ::
        (j, decl name2 value, env2) :: tail) s =
      runUnits fuel ((j, decl name2 value, env2) ::
        (i, .decl .const name1 (.logical op left.expr right), env1) :: tail) s := by
  cases fuel with
  | zero => rfl
  | succ fuel =>
    cases fuel with
    | zero => rfl
    | succ fuel =>
      cases fuel with
      | zero =>
        simp only [runUnits, M.bind_apply, run_literal 0 j env2 name2 value c2 h2]
        rfl
      | succ n =>
        simp only [runUnits, M.bind_apply, run_logical_skipped n i env1 name1 op left right c1 skips h1,
          run_literal (n + 1) j env2 name2 value c2 h2]
        have same :
            { ({ s.assign c1 left.value with region := 0 }).assign c2 value.value with region := 0 } =
              { ({ s.assign c2 value.value with region := 0 }).assign c1 left.value with region := 0 } := by
          simpa only [State.assign] using congrArg (fun t : State W => { t with region := 0 })
            (State.assign_comm s c1 c2 left.value value.value distinct)
        rw [same]

/-- Actual top-level allocation gives distinct cells to distinct resolved names. -/
theorem linked_distinct {W : World} {ss : List Stmt} {frames : List (Nat × List (Name × Addr))}
    {s s' : State W} (allocated : declareTops ((List.range ss.length).zip ss) s = .ok frames s')
    {name1 name2 : Name} (different : name1 ≠ name2) {c1 c2 : Addr}
    (h1 : (bundleEnv ss frames).vars.lookup name1 = some c1)
    (h2 : (bundleEnv ss frames).vars.lookup name2 = some c2) : c1 ≠ c2 := by
  intro same
  subst c2
  exact different (declareTops_bundleEnv_injective allocated h1 h2)

/-- The syntax and actual linked allocation justify a literal swap without
assuming that differently named bindings happen to occupy different cells. -/
theorem swap_from_link {W : World} {ss : List Stmt} {frames : List (Nat × List (Name × Addr))}
    {before after : State W} (allocated : declareTops ((List.range ss.length).zip ss) before = .ok frames after)
    (fuel i j : Nat) (name1 name2 : Name) (different : name1 ≠ name2) (v1 v2 : Literal) (c1 c2 : Addr)
    (h1 : (bundleEnv ss frames).vars.lookup name1 = some c1)
    (h2 : (bundleEnv ss frames).vars.lookup name2 = some c2) (tail : List LinkedUnit) (s : State W) :
    runUnits fuel ((i, decl name1 v1, bundleEnv ss frames) :: (j, decl name2 v2, bundleEnv ss frames) :: tail) s =
      runUnits fuel ((j, decl name2 v2, bundleEnv ss frames) :: (i, decl name1 v1, bundleEnv ss frames) :: tail) s :=
  swap_literals fuel i j _ _ name1 name2 v1 v2 c1 c2 h1 h2
    (linked_distinct allocated different h1 h2) tail s

/-- A function declaration unit performs its allocation at link time. At unit
execution time it does no work other than reset the region. -/
theorem run_function {W : World} (fuel id : Nat) (env : Env) (fn : Func) (s : State W) :
    runUnit (fuel + 1) env id (.funDecl fn) s = .ok .normal { s with region := 0 } := by
  rfl

/-- A literal declaration and a hoisted function-declaration unit commute.
This is only the unit execution step; the linker must separately preserve the
function's captured bindings when a full module plan is constructed. -/
theorem swap_literal_function {W : World} (fuel i j : Nat) (env1 env2 : Env)
    (name : Name) (v : Literal) (cell : Addr) (linked : env1.vars.lookup name = some cell)
    (fn : Func) (tail : List LinkedUnit) (s : State W) :
    runUnits fuel ((i, decl name v, env1) :: (j, .funDecl fn, env2) :: tail) s =
      runUnits fuel ((j, .funDecl fn, env2) :: (i, decl name v, env1) :: tail) s := by
  cases fuel with
  | zero => rfl
  | succ fuel =>
    cases fuel with
    | zero => cases v <;> rfl
    | succ n =>
      simp only [runUnits, M.bind_apply, run_literal n i env1 name v cell linked,
        run_function (n + 1) j env2 fn]
      rfl

/-- A shared prefix may itself end abruptly; in that case the swap is never reached. -/
theorem swap_in_context {W : World} (fuel i j : Nat) (env1 env2 : Env)
    (name1 name2 : Name) (v1 v2 : Literal) (c1 c2 : Addr)
    (h1 : env1.vars.lookup name1 = some c1) (h2 : env2.vars.lookup name2 = some c2)
    (distinct : c1 ≠ c2) (pre tail : List LinkedUnit) (s : State W) :
    runUnits fuel (pre ++ (i, decl name1 v1, env1) :: (j, decl name2 v2, env2) :: tail) s =
      runUnits fuel (pre ++ (j, decl name2 v2, env2) :: (i, decl name1 v1, env1) :: tail) s := by
  induction pre generalizing s with
  | nil => exact swap_literals fuel i j env1 env2 name1 name2 v1 v2 c1 c2 h1 h2 distinct tail s
  | cons u pre ih =>
    obtain ⟨id, stmt, env⟩ := u
    simp only [List.cons_append, runUnits, M.bind_apply]
    cases h : runUnit fuel env id stmt s with
    | ok value state => exact ih state
    | throw value state => rfl
    | unknown why state => rfl
    | fuel => rfl

/-- Lift the literal/function swap through any shared earlier units. -/
theorem swap_function_in_context {W : World} (fuel i j : Nat) (env1 env2 : Env)
    (name : Name) (v : Literal) (cell : Addr) (linked : env1.vars.lookup name = some cell)
    (fn : Func) (pre tail : List LinkedUnit) (s : State W) :
    runUnits fuel (pre ++ (i, decl name v, env1) :: (j, .funDecl fn, env2) :: tail) s =
      runUnits fuel (pre ++ (j, .funDecl fn, env2) :: (i, decl name v, env1) :: tail) s := by
  induction pre generalizing s with
  | nil => exact swap_literal_function fuel i j env1 env2 name v cell linked fn tail s
  | cons u pre ih =>
    obtain ⟨id, stmt, env⟩ := u
    simp only [List.cons_append, runUnits, M.bind_apply]
    cases h : runUnit fuel env id stmt s with
    | ok value state => exact ih state
    | throw value state => rfl
    | unknown why state => rfl
    | fuel => rfl

/-- A finite sequence of independently justified adjacent swaps. -/
inductive SafeReorder : List LinkedUnit → List LinkedUnit → Prop where
  | refl (units) : SafeReorder units units
  | swap (pre tail : List LinkedUnit) (i j : Nat) (env1 env2 : Env)
      (name1 name2 : Name) (v1 v2 : Literal) (c1 c2 : Addr)
      (h1 : env1.vars.lookup name1 = some c1) (h2 : env2.vars.lookup name2 = some c2)
      (distinct : c1 ≠ c2) :
      SafeReorder (pre ++ (i, decl name1 v1, env1) :: (j, decl name2 v2, env2) :: tail)
        (pre ++ (j, decl name2 v2, env2) :: (i, decl name1 v1, env1) :: tail)
  | swapFunction (pre tail : List LinkedUnit) (i j : Nat) (env1 env2 : Env)
      (name : Name) (v : Literal) (cell : Addr) (linked : env1.vars.lookup name = some cell) (fn : Func) :
      SafeReorder (pre ++ (i, decl name v, env1) :: (j, .funDecl fn, env2) :: tail)
        (pre ++ (j, .funDecl fn, env2) :: (i, decl name v, env1) :: tail)
  | symm {a b} : SafeReorder a b → SafeReorder b a
  | trans {a b c} : SafeReorder a b → SafeReorder b c → SafeReorder a c

/-- Each step preserves the complete result, so the entire finite schedule does too. -/
theorem reorder_exact {W : World} (fuel : Nat) {before after : List LinkedUnit}
    (safe : SafeReorder before after) (s : State W) : runUnits fuel before s = runUnits fuel after s := by
  induction safe with
  | refl => rfl
  | swap pre tail i j env1 env2 name1 name2 v1 v2 c1 c2 h1 h2 distinct =>
    exact swap_in_context fuel i j env1 env2 name1 name2 v1 v2 c1 c2 h1 h2 distinct pre tail s
  | swapFunction pre tail i j env1 env2 name v cell linked fn =>
    exact swap_function_in_context fuel i j env1 env2 name v cell linked fn pre tail s
  | symm _ ih => exact ih.symm
  | trans _ _ ih1 ih2 => exact ih1.trans ih2

/-- Lift a sequence of checked local swaps to the actual `splitProgram` of a
plan. The witnesses describe the real links, common initial state, and scope
agreement; no execution result or equivalence is assumed. -/
theorem preserve_plan {W : World} {source : Program} {ss : List Stmt}
    {bundle : EsmSplit.Bundle} {plan : EsmSplit.Plan} (fuel : Nat)
    (before : LinkedBehavior.CheckedLink source W)
    (after : LinkedBehavior.CheckedLink (splitProgram ss bundle plan) W)
    (initial : before.ready = after.ready) (middle : List LinkedUnit)
    (moves : SafeReorder (unitsOf before.linked) middle)
    (scopes : SameUnits middle (unitsOf after.linked)) :
    runProgram W fuel source = runProgram W fuel (splitProgram ss bundle plan) := by
  rw [LinkedBehavior.run_after_link fuel before, LinkedBehavior.run_after_link fuel after, ← initial]
  exact (reorder_exact fuel moves before.ready).trans (congrFun (runUnits_same fuel scopes) before.ready)

namespace CorpusCrossing

/-! The resolver identifies #1 and #2 as the extracted range and #0 as
residual in the public `emission/syntactic_holes/outer_statement_runs/spec`
case. The actual function body and all later units are left arbitrary here.
This proves the two execution-unit inversions, not the complete source-to-plan
link or a public certificate. The full plan still needs the function's captured
scope, live exports, and host observations checked. -/

def ignored : Stmt := decl "ignoredBefore" (.str "before")
def first : Stmt := decl "runtimeFirst" (.str "alpha")

theorem resolvedMoves (env : Env) (fn : Func) (c0 c1 : Addr)
    (h0 : env.vars.lookup "ignoredBefore" = some c0)
    (h1 : env.vars.lookup "runtimeFirst" = some c1) (distinct : c0 ≠ c1)
    (tail : List LinkedUnit) :
    SafeReorder ((0, ignored, env) :: (1, first, env) :: (2, .funDecl fn, env) :: tail)
      ((1, first, env) :: (2, .funDecl fn, env) :: (0, ignored, env) :: tail) := by
  apply SafeReorder.trans
    (SafeReorder.swap [] ((2, .funDecl fn, env) :: tail) 0 1 env env
      "ignoredBefore" "runtimeFirst" (.str "before") (.str "alpha") c0 c1 h0 h1 distinct)
  exact SafeReorder.swapFunction [(1, first, env)] tail 0 2 env env
    "ignoredBefore" (.str "before") c0 h0 fn

/-- Every bounded outcome is preserved across those two crossed units, for any
already-linked state and any common suffix. -/
theorem resolvedExecution {W : World} (fuel : Nat) (env : Env) (fn : Func) (c0 c1 : Addr)
    (h0 : env.vars.lookup "ignoredBefore" = some c0)
    (h1 : env.vars.lookup "runtimeFirst" = some c1) (distinct : c0 ≠ c1)
    (tail : List LinkedUnit) (s : State W) :
    runUnits fuel ((0, ignored, env) :: (1, first, env) :: (2, .funDecl fn, env) :: tail) s =
      runUnits fuel ((1, first, env) :: (2, .funDecl fn, env) :: (0, ignored, env) :: tail) s :=
  reorder_exact fuel (resolvedMoves env fn c0 c1 h0 h1 distinct tail) s

/-- Lift the resolved pair of inversions to a concrete plan after its real links,
initialization state, and the free-name scopes (including the function capture)
have been established. The caller still has to establish those link facts for
its source and plan; this does not compare their candidate executions. -/
theorem resolvedPlan {W : World} {original : Program} {ss : List Stmt}
    {bundle : EsmSplit.Bundle} {plan : EsmSplit.Plan} (fuel : Nat)
    (before : LinkedBehavior.CheckedLink original W)
    (after : LinkedBehavior.CheckedLink (splitProgram ss bundle plan) W)
    (initial : before.ready = after.ready) (env : Env) (fn : Func) (c0 c1 : Addr)
    (h0 : env.vars.lookup "ignoredBefore" = some c0)
    (h1 : env.vars.lookup "runtimeFirst" = some c1) (distinct : c0 ≠ c1)
    (tail : List LinkedUnit)
    (sourceUnits : unitsOf before.linked =
      (0, ignored, env) :: (1, first, env) :: (2, .funDecl fn, env) :: tail)
    (scopes : SameUnits
      ((1, first, env) :: (2, .funDecl fn, env) :: (0, ignored, env) :: tail)
      (unitsOf after.linked)) :
    runProgram W fuel original = runProgram W fuel (splitProgram ss bundle plan) := by
  apply preserve_plan fuel before after initial _ ?_ scopes
  rw [sourceUnits]
  exact resolvedMoves env fn c0 c1 h0 h1 distinct tail


end CorpusCrossing

namespace AuthoredFixture

/-- The authored placement of public fixture 8ccb05394d872465. -/
def spec : EsmSplit.Spec := {
  entry := "entry.mjs"
  modules := [("entry.mjs", [.stmt 2]), ("left.mjs", [.stmt 0]), ("right.mjs", [.stmt 1])]
  order := ["right.mjs", "left.mjs"]
  sequential := true
}

def bundle : EsmSplit.Bundle := [
  { src := "", defs := ["a"] },
  { src := "", defs := ["b"] },
  { src := "", defs := ["result"], eager := ["a"], exports := [("result", some "result")] }]

def stmts (a b : Literal) : List Stmt := [decl "a" a, decl "b" b, .decl .const "result" (.var "a")]
def source (a b : Literal) : Program :=
  ⟨"entry.mjs", [⟨"entry.mjs", [], [(0, decl "a" a), (1, decl "b" b),
    (2, .decl .const "result" (.var "a"))], [("result", "result")]⟩]⟩

def plan : EsmSplit.Plan := EsmSplit.mkPlan bundle spec
def output (a b : Literal) : Program := splitProgram (stmts a b) bundle plan

private def sourceEnv : Env :=
  ⟨[("a", ⟨1, 0⟩), ("b", ⟨2, 0⟩), ("result", ⟨3, 0⟩)], .undef, none⟩
private def rightEnv : Env := ⟨[("b", ⟨2, 0⟩)], .undef, none⟩
private def leftEnv : Env := ⟨[("a", ⟨1, 0⟩)], .undef, none⟩
private def entryEnv : Env := ⟨[("result", ⟨3, 0⟩), ("a", ⟨1, 0⟩)], .undef, none⟩
private def ready (w : World) : State w :=
  let frame : Array Binding := #[⟨none, false⟩]
  { initialState w 3 with cells := #[#[], frame, frame, frame] }
private def sourceLinked (a b : Literal) : List (Module × Env) :=
  [(⟨"entry.mjs", [], [(0, decl "a" a), (1, decl "b" b),
    (2, .decl .const "result" (.var "a"))], [("result", "result")]⟩, sourceEnv)]
private def outputLinked (a b : Literal) : List (Module × Env) :=
  [(⟨"right.mjs", [], [(1, decl "b" b)], []⟩, rightEnv),
   (⟨"left.mjs", [], [(0, decl "a" a)], [("a", "a")]⟩, leftEnv),
   (⟨"entry.mjs", [⟨"right.mjs", []⟩, ⟨"left.mjs", [("a", "a")]⟩],
     [(2, .decl .const "result" (.var "a"))], [("result", "result")]⟩, entryEnv)]
private def sourceLink (w : World) (a b : Literal) : LinkedBehavior.CheckedLink (source a b) w :=
  ⟨sourceLinked a b, ready w, by cbv⟩
private def outputLink (w : World) (a b : Literal) : LinkedBehavior.CheckedLink (output a b) w :=
  ⟨outputLinked a b, ready w, by cbv⟩

private theorem leftAgree (a : Literal) : Agree (decl "a" a).free sourceEnv leftEnv := by
  refine ⟨rfl, rfl, ?_⟩
  simp [decl, Stmt.free, literal_closed, sourceEnv, leftEnv] <;> decide

private theorem rightAgree (b : Literal) : Agree (decl "b" b).free sourceEnv rightEnv := by
  refine ⟨rfl, rfl, ?_⟩
  simp [decl, Stmt.free, literal_closed, sourceEnv, rightEnv] <;> decide

private theorem entryAgree : Agree (Stmt.decl .const "result" (.var "a")).free sourceEnv entryEnv := by
  refine ⟨rfl, rfl, ?_⟩
  simp [Stmt.free, Expr.free, sourceEnv, entryEnv] <;> decide

/-- The actual requested planner reverses the two independent units. -/
theorem authoredOrder : plan.execOrder = [1, 0, 2] ∧ plan.evalOrder = ["right.mjs", "left.mjs", "entry.mjs"] := by
  constructor <;> decide

/-- The planner preserves the authored owner for each source unit. -/
theorem authoredPlacement (a b : Literal) :
    (output a b).modules.map (fun m => (m.name, m.body.map Prod.fst)) =
      [("entry.mjs", [2]), ("left.mjs", [0]), ("right.mjs", [1])] := by
  rfl

/-- The constructed plan preserves the complete bounded outcome for every world. -/
theorem authoredAllOutcomes (a b : Literal) (w : World) (fuel : Nat) :
    runProgram w fuel (source a b) = runProgram w fuel (output a b) := by
  have schedule : SafeReorder (unitsOf (sourceLink w a b).linked)
      [(1, decl "b" b, sourceEnv), (0, decl "a" a, sourceEnv),
        (2, .decl .const "result" (.var "a"), sourceEnv)] := by
    exact .swap [] [(2, .decl .const "result" (.var "a"), sourceEnv)] 0 1 sourceEnv sourceEnv
      "a" "b" a b ⟨1, 0⟩ ⟨2, 0⟩ rfl rfl (by decide)
  have scopes : SameUnits
      [(1, decl "b" b, sourceEnv), (0, decl "a" a, sourceEnv),
        (2, .decl .const "result" (.var "a"), sourceEnv)]
      (unitsOf (outputLink w a b).linked) := by
    exact .cons ⟨rfl, rfl, rightAgree b⟩ (.cons ⟨rfl, rfl, leftAgree a⟩
      (.cons ⟨rfl, rfl, entryAgree⟩ .nil))
  exact preserve_plan fuel (sourceLink w a b) (outputLink w a b) rfl _ schedule scopes

/-- The public result resolves to the same live cell. -/
theorem sameExports (a b : Literal) : LinkedBehavior.SameExports (source a b) (output a b) := by
  intro w
  refine ⟨[("result", ⟨3, 0⟩)], [("result", ⟨3, 0⟩)], ?_, ?_, List.Perm.refl _⟩
  · unfold LinkedBehavior.exports
    rw [(sourceLink w a b).equation]
    rfl
  · unfold LinkedBehavior.exports
    rw [(outputLink w a b).equation]
    rfl

private def thrownCells (r : Res World.probe Unit) : Option (Val × Option Val × Option Val) :=
  match r with
  | .throw value state => some (value, (state.cellAt ⟨1, 0⟩).bind (·.value),
      (state.cellAt ⟨2, 0⟩).bind (·.value))
  | _ => none

/-- A later throw retains both initialized cells and the thrown value in either order. -/
theorem throw_after_swap :
    thrownCells (runUnits 16 [(0, decl "a" (.bool true), sourceEnv),
      (1, decl "b" (.bool false), sourceEnv), (2, .throw (.str "stop"), sourceEnv)] (ready World.probe)) =
        some (.str "stop", some (.bool true), some (.bool false)) ∧
    thrownCells (runUnits 16 [(1, decl "b" (.bool false), sourceEnv),
      (0, decl "a" (.bool true), sourceEnv), (2, .throw (.str "stop"), sourceEnv)] (ready World.probe)) =
        some (.str "stop", some (.bool true), some (.bool false)) := by
  constructor <;> cbv

/-- A read of the crossed binding is not a literal initializer. -/
theorem read_not_admitted (value : Literal) : decl "b" value ≠ .decl .const "b" (.var "a") := by
  cases value <;> simp [decl, Literal.expr]

private def ending (r : Res World.probe Unit) : String :=
  match r with
  | .ok .. => "normal"
  | .throw (.error .reference message) _ => message
  | .throw .. => "other throw"
  | .unknown .. => "unknown"
  | .fuel => "fuel"

/-- Moving a read ahead of its initializing declaration changes a normal
completion into a TDZ throw, so it cannot use the literal swap rule. -/
theorem read_before_initialization :
    ending (runUnits 16 [(0, decl "a" (.bool true), sourceEnv),
      (1, .decl .const "b" (.var "a"), sourceEnv)] (ready World.probe)) = "normal" ∧
    ending (runUnits 16 [(1, .decl .const "b" (.var "a"), sourceEnv),
      (0, decl "a" (.bool true), sourceEnv)] (ready World.probe)) =
        "cannot access 'a' before initialization" := by
  constructor <;> cbv


end AuthoredFixture


end CoreJS.ExactLiteralMove
