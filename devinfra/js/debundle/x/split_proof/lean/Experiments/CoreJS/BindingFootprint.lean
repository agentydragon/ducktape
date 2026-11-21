import Experiments.CoreJS.BindingCertificate
import Experiments.CoreJS.CellFootprint

/-!
# Semantic binding footprints for the certified syntax

For the fragment accepted by `BindingCertificate`, every successful evaluation can change only the bindings named
by its summary and replays when those complete binding records agree. The address predicate follows the environment,
so reads and writes of a name resolve to the exact `Addr` used by the CoreJS evaluator. No restriction is placed on
binding values, initialization state, mutability, or the objects those values reference.
-/

namespace CoreJS

/-- Addresses of names in an environment, represented by their resolved binding cells. -/
def bindingAddresses (env : Env) (names : List Name) : Addr → Prop :=
  fun a => ∃ x, x ∈ names ∧ env.vars.lookup x = some a

namespace BindingSummary

def readCells (env : Env) (summary : BindingSummary) : Addr → Prop :=
  bindingAddresses env summary.reads

def writeCells (env : Env) (summary : BindingSummary) : Addr → Prop :=
  bindingAddresses env summary.writes

end BindingSummary

theorem bindingAddresses_append_iff (env : Env) (xs ys : List Name) (a : Addr) :
    bindingAddresses env (xs ++ ys) a ↔ bindingAddresses env xs a ∨ bindingAddresses env ys a := by
  constructor
  · rintro ⟨x, hx, hlookup⟩
    rcases List.mem_append.mp hx with hx | hy
    · exact Or.inl ⟨x, hx, hlookup⟩
    · exact Or.inr ⟨x, hy, hlookup⟩
  · rintro (⟨x, hx, hlookup⟩ | ⟨x, hy, hlookup⟩)
    · exact ⟨x, List.mem_append_left _ hx, hlookup⟩
    · exact ⟨x, List.mem_append_right _ hy, hlookup⟩

private theorem cellFootprint_outOfFuel_mono {W : World} {α : Type} {rd wr : Addr → Prop} :
    CellFootprint rd wr (outOfFuel : M W α) := by
  exact (cellFootprint_outOfFuel (W := W) (α := α)).mono
    (fun _ h => False.elim h) (fun _ h => False.elim h)

/-- Bind two computations whose footprints are described by name lists in one fixed environment. -/
private theorem bindBindingFootprints {W : World} {α β : Type} (env : Env)
    (reads writes reads' writes' : List Name) {m : M W α} {f : α → M W β}
    (hm : CellFootprint (bindingAddresses env reads) (bindingAddresses env writes) m)
    (hf : ∀ a, CellFootprint (bindingAddresses env reads') (bindingAddresses env writes') (f a)) :
    CellFootprint (bindingAddresses env (reads ++ reads')) (bindingAddresses env (writes ++ writes')) (m >>= f) := by
  apply (hm.bind hf).mono
  · intro a ha
    exact (bindingAddresses_append_iff env reads reads' a).2 ha
  · intro a ha
    exact (bindingAddresses_append_iff env writes writes' a).2 ha

private theorem cellFootprint_pure_mono {W : World} {α : Type} {rd wr : Addr → Prop} (a : α) :
    CellFootprint (W := W) rd wr (pure a : M W α) := by
  exact (cellFootprint_pure (W := W) a).mono
    (fun _ h => False.elim h) (fun _ h => False.elim h)

private theorem writeVar_then_pure {W : World} (env : Env) (x : Name) (v : Val) :
    CellFootprint (W := W) (bindingAddresses env []) (bindingAddresses env [x])
      ((writeVar env x v >>= fun _ => pure v) : M W Val) := by
  have h := (cellFootprint_writeVar (W := W) env x v).bind
    (fun _ => cellFootprint_pure (W := W) v)
  exact h.mono
    (by intro a h; cases h with | inl h => exact False.elim h | inr h => exact False.elim h)
    (by
      intro a h
      cases h with
      | inl h => exact ⟨x, by simp, h⟩
      | inr h => exact False.elim h)

private theorem writeVar_then_normal {W : World} (env : Env) (x : Name) (v : Val) :
    CellFootprint (W := W) (bindingAddresses env []) (bindingAddresses env [x])
      ((writeVar env x v >>= fun _ => pure Flow.normal) : M W Flow) := by
  have h := (cellFootprint_writeVar (W := W) env x v).bind
    (fun _ => cellFootprint_pure (W := W) Flow.normal)
  exact h.mono
    (by intro a h; cases h with | inl h => exact False.elim h | inr h => exact False.elim h)
    (by
      intro a h
      cases h with
      | inl h => exact ⟨x, by simp, h⟩
      | inr h => exact False.elim h)

private theorem initVar_then_normal {W : World} (env : Env) (x : Name) (v : Val) :
    CellFootprint (W := W) (bindingAddresses env []) (bindingAddresses env [x])
      ((initVar env x v >>= fun _ => pure Flow.normal) : M W Flow) := by
  have h := (cellFootprint_initVar (W := W) env x v).bind
    (fun _ => cellFootprint_pure (W := W) Flow.normal)
  exact h.mono
    (by intro a h; cases h with | inl h => exact False.elim h | inr h => exact False.elim h)
    (by
      intro a h
      cases h with
      | inl h => exact ⟨x, by simp, h⟩
      | inr h => exact False.elim h)

private theorem pure_then_normal {W : World} (env : Env) :
    CellFootprint (W := W) (bindingAddresses env []) (bindingAddresses env [])
      (pure Flow.normal : M W Flow) := by
  exact (cellFootprint_pure (W := W) Flow.normal).mono
    (by intro a h; exact False.elim h) (by intro a h; exact False.elim h)

private theorem evalExpr_assignVar {W : World} (fuel : Nat) (env : Env) (x : Name) (rhs : Expr) :
    evalExpr (W := W) (fuel + 1) env (.assign (.var x) rhs) =
      evalExpr fuel env rhs >>= fun v => writeVar env x v >>= fun _ => pure v := by
  cases fuel <;> rfl

private theorem evalExpr_seq {W : World} (fuel : Nat) (env : Env) (first second : Expr) :
    evalExpr (W := W) (fuel + 1) env (.seq first second) =
      evalExpr fuel env first >>= fun _ => evalExpr fuel env second := by
  rfl

private theorem execStmt_expr {W : World} (fuel : Nat) (env : Env) (e : Expr) :
    execStmt (W := W) (fuel + 1) env (.expr e) =
      evalExpr fuel env e >>= fun _ => pure Flow.normal := by
  rfl

private theorem execStmt_declVar {W : World} (fuel : Nat) (env : Env) (x : Name) (init : Expr) :
    execStmt (W := W) (fuel + 1) env (.decl .var x init) =
      evalExpr fuel env init >>= fun v => writeVar env x v >>= fun _ => pure Flow.normal := by
  rfl

private theorem execStmt_declInit {W : World} (fuel : Nat) (env : Env) (kind : DeclKind) (x : Name)
    (init : Expr) (hkind : kind ≠ .var) :
    execStmt (W := W) (fuel + 1) env (.decl kind x init) =
      evalExpr fuel env init >>= fun v => initVar env x v >>= fun _ => pure Flow.normal := by
  cases kind with
  | var => exact False.elim (hkind rfl)
  | let_ => rfl
  | const => rfl

/-- The semantic cell footprint of every expression admitted by `Expr.bindingSummary`. -/
private theorem exprBindingFootprintAt {W : World} (fuel : Nat) :
    ∀ (e : Expr) {summary : BindingSummary}, e.bindingSummary = some summary → ∀ env : Env,
      CellFootprint (summary.readCells env) (summary.writeCells env) (evalExpr fuel env e : M W Val) := by
  induction fuel with
  | zero =>
      intro e summary hs env
      simpa [evalExpr] using (cellFootprint_outOfFuel_mono (W := W) (α := Val)
        (rd := summary.readCells env) (wr := summary.writeCells env))
  | succ fuel ih =>
      intro e summary hs env
      cases e with
      | undef =>
          simp only [Expr.bindingSummary, Option.some.injEq] at hs
          subst summary
          simpa [evalExpr] using (cellFootprint_pure_mono (W := W)
            (rd := BindingSummary.readCells env {}) (wr := BindingSummary.writeCells env {}) Val.undef)
      | null =>
          simp only [Expr.bindingSummary, Option.some.injEq] at hs
          subst summary
          simpa [evalExpr] using (cellFootprint_pure_mono (W := W)
            (rd := BindingSummary.readCells env {}) (wr := BindingSummary.writeCells env {}) Val.null)
      | bool b =>
          simp only [Expr.bindingSummary, Option.some.injEq] at hs
          subst summary
          simpa [evalExpr] using (cellFootprint_pure_mono (W := W)
            (rd := BindingSummary.readCells env {}) (wr := BindingSummary.writeCells env {}) (Val.bool b))
      | num n' =>
          simp only [Expr.bindingSummary, Option.some.injEq] at hs
          subst summary
          simpa [evalExpr] using (cellFootprint_pure_mono (W := W)
            (rd := BindingSummary.readCells env {}) (wr := BindingSummary.writeCells env {}) (Val.num n'))
      | str text =>
          simp only [Expr.bindingSummary, Option.some.injEq] at hs
          subst summary
          simpa [evalExpr] using (cellFootprint_pure_mono (W := W)
            (rd := BindingSummary.readCells env {}) (wr := BindingSummary.writeCells env {}) (Val.str text))
      | var x =>
          simp only [Expr.bindingSummary, Option.some.injEq] at hs
          subst summary
          have h := (cellFootprint_readVar (W := W) env x).mono
            (rd' := bindingAddresses env [x]) (wr' := bindingAddresses env [])
            (by intro a h; exact ⟨x, by simp, h⟩)
            (by intro a h; exact False.elim h)
          simpa [evalExpr, BindingSummary.readCells, BindingSummary.writeCells] using h
      | assign target rhs =>
          cases target with
          | var x =>
              cases hrhs : rhs.bindingSummary with
              | none => simp [Expr.bindingSummary, hrhs] at hs
              | some rhsSummary =>
                  have hsummary : BindingSummary.seq rhsSummary { writes := [x] } = summary := by
                    simpa [Expr.bindingSummary, hrhs] using hs
                  subst summary
                  have hr := ih rhs hrhs env
                  have hc := bindBindingFootprints env rhsSummary.reads rhsSummary.writes [] [x] hr
                    (fun v => writeVar_then_pure (W := W) env x v)
                  rw [evalExpr_assignVar (W := W)]
                  simpa [BindingSummary.seq, BindingSummary.readCells,
                    BindingSummary.writeCells] using hc
          | frozen _ => simp [Expr.bindingSummary] at hs
          | member _ _ => simp [Expr.bindingSummary] at hs
      | seq first second =>
          cases hfirst : first.bindingSummary with
          | none => simp [Expr.bindingSummary, hfirst] at hs
          | some firstSummary =>
              cases hsecond : second.bindingSummary with
              | none => simp [Expr.bindingSummary, hfirst, hsecond] at hs
              | some secondSummary =>
                  have hsummary : BindingSummary.seq firstSummary secondSummary = summary := by
                    simpa [Expr.bindingSummary, hfirst, hsecond] using hs
                  subst summary
                  have hfirstFootprint := ih first hfirst env
                  have hsecondFootprint := ih second hsecond env
                  have hc := bindBindingFootprints env firstSummary.reads firstSummary.writes
                    secondSummary.reads secondSummary.writes hfirstFootprint
                    (fun _ => hsecondFootprint)
                  rw [evalExpr_seq (W := W)]
                  simpa [BindingSummary.seq, BindingSummary.readCells,
                    BindingSummary.writeCells] using hc
      | _ => simp [Expr.bindingSummary] at hs

/-- The semantic cell footprint of every expression admitted by `Expr.bindingSummary`. -/
theorem Expr.bindingFootprint {W : World} {e : Expr} {summary : BindingSummary}
    (hs : e.bindingSummary = some summary) (fuel : Nat) (env : Env) :
    CellFootprint (summary.readCells env) (summary.writeCells env) (evalExpr fuel env e : M W Val) :=
  exprBindingFootprintAt fuel e hs env

/-- The semantic cell footprint of every statement admitted by `Stmt.bindingSummary`. -/
theorem Stmt.bindingFootprint {W : World} {s : Stmt} {summary : BindingSummary}
    (hs : s.bindingSummary = some summary) (fuel : Nat) (env : Env) :
    CellFootprint (summary.readCells env) (summary.writeCells env) (execStmt fuel env s : M W Flow) := by
  cases s with
  | expr e =>
    cases he : e.bindingSummary with
    | none => simp [Stmt.bindingSummary, he] at hs
    | some exprSummary =>
      have hsummary : exprSummary = summary := by simpa [Stmt.bindingSummary, he] using hs
      subst summary
      cases fuel with
      | zero =>
        simpa [execStmt] using (cellFootprint_outOfFuel_mono (W := W) (α := Flow)
          (rd := exprSummary.readCells env) (wr := exprSummary.writeCells env))
      | succ n =>
        have heval := Expr.bindingFootprint (W := W) he n env
        have hc := bindBindingFootprints env exprSummary.reads exprSummary.writes [] [] heval
          (fun _ => pure_then_normal (W := W) env)
        rw [execStmt_expr (W := W)]
        simpa [List.append_nil, BindingSummary.readCells, BindingSummary.writeCells] using hc
  | decl kind x init =>
    cases hi : init.bindingSummary with
    | none => simp [Stmt.bindingSummary, hi] at hs
    | some initSummary =>
      have hsummary : BindingSummary.seq initSummary { writes := [x] } = summary := by
        simpa [Stmt.bindingSummary, hi] using hs
      cases fuel with
      | zero =>
        simpa [execStmt] using (cellFootprint_outOfFuel_mono (W := W) (α := Flow)
          (rd := summary.readCells env) (wr := summary.writeCells env))
      | succ n =>
        subst summary
        have heval := Expr.bindingFootprint (W := W) hi n env
        cases kind with
        | var =>
          have hc := bindBindingFootprints env initSummary.reads initSummary.writes [] [x] heval
            (fun v => writeVar_then_normal (W := W) env x v)
          rw [execStmt_declVar (W := W)]
          simpa [BindingSummary.seq, BindingSummary.readCells, BindingSummary.writeCells] using hc
        | let_ =>
          have hc := bindBindingFootprints env initSummary.reads initSummary.writes [] [x] heval
            (fun v => initVar_then_normal (W := W) env x v)
          rw [execStmt_declInit (W := W) n env .let_ x init (by decide)]
          simpa [BindingSummary.seq, BindingSummary.readCells, BindingSummary.writeCells] using hc
        | const =>
          have hc := bindBindingFootprints env initSummary.reads initSummary.writes [] [x] heval
            (fun v => initVar_then_normal (W := W) env x v)
          rw [execStmt_declInit (W := W) n env .const x init (by decide)]
          simpa [BindingSummary.seq, BindingSummary.readCells, BindingSummary.writeCells] using hc
  | _ => simp [Stmt.bindingSummary] at hs

#print axioms Expr.bindingFootprint
#print axioms Stmt.bindingFootprint

end CoreJS
