import EsmSplit.CoreJS.ClassParents
import EsmSplit.CoreJS.LinkWorld
import EsmSplit.CoreJS.RecordOrigin
import EsmSplit.CoreJS.LinkedCells

/-! After a schedule check fails, prove selected own reads against the two
actual linked prefixes. The replacements exist only in the scheduling proof.
Unrecognized operations forget the whole table, including aliased receivers. -/

namespace CoreJS.PrefixProjection

private inductive Shape where
  | number (value : Float)
  | text (value : String)
  | numbers (fields : List (String × Float))

private def Shape.Holds (s : State w) : Shape → Val → Prop
  | .number x, v => v = .num x
  | .text str, v => v = .str str
  | .numbers fields, v => ∃ a o, v = .obj a ∧ s.objAt a = some o ∧
      ∀ k x, fields.lookup k = some x → o.props.lookup (.str k) = some (.data (.num x) true true true)

private theorem Shape.transport {s t : State w} {shape : Shape} (h : shape.Holds s v)
    (objects : ∀ a o, s.objAt a = some o → t.objAt a = some o) : shape.Holds t v := by
  cases shape with
  | number | text => exact h
  | numbers =>
    obtain ⟨a, o, value, atObj, fields⟩ := h
    exact ⟨a, o, value, objects _ _ atObj, fields⟩

private structure Facts where
  live : Addr → Bool
  entries : List (Addr × Shape) := []

private structure Holds (p : Facts) (s : State w) : Prop where
  live : ∀ c, p.live c = true → (s.cellAt c).isSome
  entry : ∀ c sh, (c, sh) ∈ p.entries → ∃ v m, s.cellAt c = some ⟨some v, m⟩ ∧ sh.Holds s v

private def empty : Facts := ⟨fun _ => false, []⟩

private theorem empty_holds (s : State w) : Holds empty s :=
  ⟨by simp [empty], by simp [empty]⟩

private theorem Holds.region {s : State w} (h : Holds p s) (r : Nat) : Holds p {s with region := r} := by
  refine ⟨h.live, ?_⟩
  intro c sh mem
  obtain ⟨v, m, cell, value⟩ := h.entry c sh mem
  exact ⟨v, m, cell, sh.transport value (fun _ _ h => h)⟩

private theorem Holds.read {s : State w} (h : Holds p s)
    (lk : env.vars.lookup x = some c) (fact : p.entries.lookup c = some sh) :
    ∃ v, readVar env x s = .ok v s ∧ sh.Holds s v := by
  obtain ⟨v, m, bound, hs⟩ := h.entry c sh (lookup_mem fact)
  exact ⟨v, by simp only [readVar, lk, M.bind_apply, getState, bound]; rfl, hs⟩

/-- Only normal paths continue through a prefix. A read replacement has to
agree even on failure, at the same evaluator fuel. -/
private structure ExprStep (p : Facts) (env : Env) (e : Expr) where
  replacement : Expr
  shape : Option Shape
  same : ∀ {w} n (s : State w), Holds p s → evalExpr n env e s = evalExpr n env replacement s
  result : ∀ {w} n (s t : State w) v, Holds p s → evalExpr n env e s = .ok v t →
    Extends s t ∧ ∀ sh, shape = some sh → sh.Holds t v

private def keep (p : Facts) (env : Env) (e : Expr) (h : e.allocOnly = true) : ExprStep p env e :=
  ⟨e, none, fun _ _ _ => rfl, by
    intro w n s t v _ run
    have preserves := allocOnly_extends (fuel := n) (env := env) (s := s) h
    change (evalExpr n env e s).Keeps s at preserves
    rw [run] at preserves
    exact ⟨preserves, by simp⟩⟩

private def found? (o : Option α) : Option ((v : α) × PLift (o = some v)) :=
  match o with
  | some v => some ⟨v, ⟨rfl⟩⟩
  | none => none

/-- For computed keys only the exact input string cells matter. Creating
data properties may change the fresh object, but cannot change those cells. -/
private structure Key (p : Facts) (env : Env) (e : Expr) where
  text : String
  run : ∀ {w} n (base s : State w), Holds p base → s.cells = base.cells →
    evalExpr (n + 1) env e s = .ok (.str text) s

private def key? (p : Facts) (env : Env) : (e : Expr) → Option (Key p env e)
  | .str k => some ⟨k, fun _ _ _ _ _ => rfl⟩
  | .var x => do
    let ⟨c, ⟨lk⟩⟩ ← found? (env.vars.lookup x)
    let ⟨.text k, ⟨fact⟩⟩ ← found? (p.entries.lookup c) | none
    return ⟨k, by
      intro w n base s h same
      obtain ⟨v, mutable, hb, equals⟩ := h.entry c _ (lookup_mem fact)
      have cell : s.cellAt c = some ⟨some v, mutable⟩ := by simpa [State.cellAt, same] using hb
      change v = .str k at equals
      subst v
      simp only [evalExpr, readVar, lk, M.bind_apply, getState, cell]; rfl⟩
  | _ => none

private theorem createData_cells {s t : State w} (run : createData a k v s = .ok () t) : t.cells = s.cells := by
  unfold createData defineOwn at run
  obtain ⟨o, s1, gets, rest⟩ := bind_ok run
  unfold getObj at gets
  split at gets <;> try contradiction
  cases gets
  split at rest <;> try contradiction
  obtain ⟨opt, s2, desc, act⟩ := bind_ok rest
  have liftState {α} (z : Except Abort α) {a : α} {before after : State w}
      (h : lift z before = .ok a after) : after = before := by
    cases z with
    | error err => cases err <;> cases h
    | ok => cases h; rfl
  obtain rfl := liftState _ desc
  cases opt with
  | none => contradiction
  | some prop => cases act; rfl

private structure Cooked (p : Facts) (env : Env) (raw : List PropDef) where
  fields : List (String × Float)
  run : ∀ {w} n a (base s t : State w), Holds p base → s.cells = base.cells →
    evalProps n env a raw s = .ok () t →
    evalProps n env a (fields.map RecordOrigin.numericField) s = .ok () t

private def cooked? (p : Facts) (env : Env) : (raw : List PropDef) → Option (Cooked p env raw)
  | [] => some ⟨[], fun _ _ _ _ _ _ _ h => h⟩
  | .init e (.num x) :: raw => do
    let key ← key? p env e
    let tail ← cooked? p env raw
    return ⟨(key.text, x) :: tail.fields, by
      intro w fuel a base s t hs same run
      cases fuel with
      | zero => cases run
      | succ n =>
        cases n with
        | zero => cases run
        | succ n =>
          have useKey := key.run n base s hs same
          simp only [evalProps, M.bind_apply, useKey] at run
          change (createData a (.str key.text) (.num x) >>= fun _ => evalProps (n + 1) env a raw) s = .ok () t at run
          obtain ⟨_, mid, put, rest⟩ := bind_ok run
          change (createData a (.str key.text) (.num x) >>= fun _ =>
            evalProps (n + 1) env a (tail.fields.map RecordOrigin.numericField)) s = .ok () t
          rw [bind_of_ok put]
          exact tail.run _ _ base mid t hs ((createData_cells put).trans same) rest⟩
  | _ => none

private theorem cookedObject {s t : State w} (c : Cooked p env ps) (hs : Holds p s)
    (run : evalExpr n env (.obj ps) s = .ok v t) :
    evalExpr n env (.obj (c.fields.map RecordOrigin.numericField)) s = .ok v t := by
  cases n with
  | zero => cases run
  | succ n =>
    rw [evalExpr] at run ⊢
    obtain ⟨a, mid, allocates, rest⟩ := bind_ok run
    obtain ⟨_, last, fields, result⟩ := bind_ok rest
    have cells : mid.cells = s.cells := by
      unfold alloc at allocates; split at allocates <;> cases allocates; rfl
    rw [bind_of_ok allocates, bind_of_ok (c.run n a s mid last hs cells fields)]
    exact result

private theorem numberFields_alloc (fs : List (String × Float)) :
    allocOnlyProps (fs.map RecordOrigin.numericField) = true := by
  induction fs with
  | nil => rfl
  | cons f rest ih => exact ih

/-- Substitute only initialized Number cells. The proof expression retains
the operator tree and its fuel cost; the recorded result is available to a
subsequent declaration in this same prefix. -/
private structure Numeric (p : Facts) (env : Env) (e : Expr) where
  value : Float
  closed : Expr
  same : ∀ {w} n (s : State w), Holds p s → evalExpr n env e s = evalExpr n env closed s
  evaluates : ∀ {w} n, (evalExpr n env closed : M w Val) = outOfFuel ∨
    evalExpr (W := w) n env closed = pure (.num value)

private def numberLeaf (p : Facts) (env : Env) (e : Expr) (x : Float)
    (read : ∀ {w} n (s : State w), Holds p s → evalExpr n env e s = evalExpr n env (.num x) s) :
    Numeric p env e :=
  ⟨x, .num x, @read, by intro w n; cases n <;> first | exact Or.inl rfl | exact Or.inr rfl⟩

private def numeric? (p : Facts) (env : Env) : (e : Expr) → Option (Numeric p env e)
  | .num x => some (numberLeaf p env _ x (fun _ _ _ => rfl))
  | .var x => do
    let ⟨c, ⟨lk⟩⟩ ← found? (env.vars.lookup x)
    let ⟨.number value, ⟨fact⟩⟩ ← found? (p.entries.lookup c) | none
    return numberLeaf p env _ value (by
      intro w n s hs
      cases n with
      | zero => rfl
      | succ =>
        obtain ⟨v, read, hv⟩ := hs.read lk fact
        change v = .num value at hv
        subst v; exact read)
  | .binary op left right => do
    guard op.numberArithmetic
    let a ← numeric? p env left
    let b ← numeric? p env right
    match hb : binop op (.num a.value) (.num b.value) with
    | .ok (.num value) =>
      return ⟨value, .binary op a.closed b.closed, by
        intro w n s hs
        cases n with
        | zero => rfl
        | succ n =>
          simp only [evalExpr, M.bind_apply, a.same n s hs]
          rcases a.evaluates (w := w) n with hf | ha
          · rw [hf]; rfl
          · have pureAt : (pure (.num a.value) : M w Val) s = .ok (.num a.value) s := rfl
            simp only [ha, pureAt, b.same n s hs], by
        intro w n
        cases n with
        | zero => exact Or.inl rfl
        | succ n =>
          rw [evalExpr]
          rcases a.evaluates (w := w) n with hf | ha
          · rw [hf]; exact Or.inl rfl
          rw [ha, M.pure_bind]
          rcases b.evaluates (w := w) n with hf | hy
          · rw [hf]; exact Or.inl rfl
          rw [hy, M.pure_bind, hb]; exact Or.inr rfl⟩
    | _ => none
  | _ => none

private def Numeric.step (a : Numeric p env e) : ExprStep p env e :=
  ⟨a.closed, some (.number a.value), a.same, by
    intro w n s t v hs run
    rw [a.same n s hs] at run
    rcases a.evaluates (w := w) n with hf | hv
    · rw [hf] at run; cases run
    · rw [hv] at run; cases run
      exact ⟨Extends.refl _, fun _ h => by cases h; rfl⟩⟩

private def infer? (p : Facts) (env : Env) (e : Expr) : Option (ExprStep p env e) := do
  match he : e with
  | .num _ | .binary .. =>
    return (← numeric? p env e).step
  | .str text =>
    return ⟨e, some (.text text), fun _ _ _ => rfl, by
      intro w n s t v _ run
      rw [he] at run
      cases n with
      | zero => cases run
      | succ => cases run; exact ⟨Extends.refl _, fun _ h => by cases h; rfl⟩⟩
  | .var x =>
    let ⟨c, ⟨lk⟩⟩ ← found? (env.vars.lookup x)
    let ⟨sh, ⟨fact⟩⟩ ← found? (p.entries.lookup c)
    return ⟨e, some sh, fun _ _ _ => rfl, by
      intro w n s t v hs run
      rw [he] at run
      cases n with
      | zero => cases run
      | succ =>
        obtain ⟨value, read, hv⟩ := hs.read lk fact
        change readVar env x s = .ok v t at run
        rw [read] at run
        cases run
        exact ⟨Extends.refl _, fun _ h => by cases h; exact hv⟩⟩
  | .obj ps =>
    let c ← cooked? p env ps
    if fresh : (c.fields.map Prod.fst).Nodup then
      return ⟨e, some (.numbers c.fields), fun _ _ _ => rfl, by
        intro w n s t v hs run
        rw [he] at run
        have actual := cookedObject c hs run
        have preserves := allocOnly_extends (fuel := n) (env := env) (s := s)
          (e := .obj (c.fields.map RecordOrigin.numericField)) (numberFields_alloc c.fields)
        change (evalExpr n env (.obj (c.fields.map RecordOrigin.numericField)) s).Keeps s at preserves
        rw [actual] at preserves
        refine ⟨preserves, ?_⟩
        intro sh eq
        cases eq
        exact RecordOrigin.numericRecord_origin c.fields fresh actual⟩
    none
  | .get (.var x) (.str key) =>
    let ⟨c, ⟨lk⟩⟩ ← found? (env.vars.lookup x)
    let ⟨.numbers fs, ⟨fact⟩⟩ ← found? (p.entries.lookup c) | none
    let ⟨number, ⟨field⟩⟩ ← found? (fs.lookup key)
    -- Keep the original depth: a literal alone would succeed at fuel 1.
    let rhs := Expr.seq .undef (.num number)
    have same {w} n (s : State w) (h : Holds p s) : evalExpr n env e s = evalExpr n env rhs s := by
      obtain ⟨v, read, a, o, rfl, obj, fields⟩ := h.read lk fact
      rw [he]
      cases n with
      | zero => rfl
      | succ n =>
        cases n with
        | zero => rfl
        | succ n =>
          have prop := getProp_ownData (callFn (W := w) (n + 1)) obj (fields _ _ field)
          simp only [rhs, evalExpr, M.pure_bind, M.bind_apply, read, prop]
          rfl
    return ⟨rhs, none, @same, by
      intro w n s t v h run
      rw [same n s h] at run
      cases n with
      | zero => cases run
      | succ n =>
        cases n with
        | zero => cases run
        | succ => cases run; exact ⟨Extends.refl _, by simp⟩⟩
  | _ => pure ()
  none

private def expr? (p : Facts) (env : Env) (e : Expr) := infer? p env e <|>
  if h : e.allocOnly = true then some (keep p env e h) else none

/-- Keep the last 32 proved bindings; evicting a fact can only refuse a read. -/
private def updated (p : Facts) (c : Addr) (info : Option Shape) : Facts :=
  { p with entries := ((info.map (c, ·)).toList ++ p.entries.filter (fun entry => entry.1 != c)).take 32 }

private def assignDecl (env : Env) (kind : DeclKind) (x : Name) (value : Val) : M w Unit :=
  match kind with
  | .var => writeVar env x value
  | _ => initVar env x value

private theorem execDecl (env : Env) (kind : DeclKind) (x : Name) (e : Expr) (n : Nat) :
    (execStmt (n + 1) env (.decl kind x e) : M w Flow) = (do
      assignDecl env kind x (← evalExpr n env e); pure .normal) := by cases kind <;> rfl

private theorem assigned {s t : State w} {env : Env} {x : Name} {value : Val} {c : Addr}
    (lk : env.vars.lookup x = some c) (kind : DeclKind)
    (run : assignDecl env kind x value s = .ok () t) :
    t = s.assign c value ∧ (kind = .var → (s.cellAt c).isSome) := by
  cases kind with
  | let_ | «const» =>
    simp only [assignDecl, initVar, lk] at run
    cases run; exact ⟨rfl, by simp⟩
  | var =>
    simp only [assignDecl, writeVar, lk, M.bind_apply, getState] at run
    cases hb : s.cellAt c with
    | none => simp [hb, throwErr, throwVal] at run
    | some b =>
      obtain ⟨v, m⟩ := b
      cases v <;> cases m <;> simp only [hb] at run
      all_goals first | cases run; exact ⟨rfl, by simp⟩ | contradiction

private structure Step (p : Facts) (u : LinkedUnit) where
  stmt : Stmt
  after : Facts
  same : ∀ {w} n (s : State w), Holds p s → execStmt n u.env u.stmt s = execStmt n u.env stmt s
  post : ∀ {w} n (s t : State w) f, Holds p s → execStmt n u.env u.stmt s = .ok f t → Holds after t

private def declStep (p : Facts) (id : Nat) (env : Env) (kind : DeclKind) (x : Name) (e : Expr)
    (c : Addr) (lk : env.vars.lookup x = some c) (ex : ExprStep p env e) :
    Step p ⟨id, .decl kind x e, env⟩ :=
  let info := if kind == .var || p.live c then ex.shape else none
  ⟨.decl kind x ex.replacement, updated p c info, by
    intro w n s h
    cases n with
    | zero => rfl
    | succ n =>
      change execStmt _ env (.decl kind x e) s = execStmt _ env (.decl kind x ex.replacement) s
      simp only [execDecl, M.bind_apply, ex.same n s h], by
    intro w n s t f hs run
    cases n with
    | zero => cases run
    | succ n =>
      rw [show execStmt (W := w) (n + 1) env (.decl kind x e) = _ from execDecl ..] at run
      obtain ⟨v, mid, he, rest⟩ := bind_ok run
      obtain ⟨_, u, hw, hf⟩ := bind_ok rest
      cases hf
      obtain ⟨ext, described⟩ := ex.result n s mid v hs he
      obtain ⟨rfl, varExists⟩ := assigned lk kind hw
      constructor
      · intro d hd
        obtain ⟨b, hb⟩ := Option.isSome_iff_exists.mp (hs.live d hd)
        by_cases eq : d = c
        · subst d; simp [State.cellAt_assign_self, ext.cells _ _ hb]
        · simp [State.cellAt_assign_other _ _ _ _ eq, ext.cells _ _ hb]
      · intro d sh mem
        have hm := List.mem_of_mem_take (show (d, sh) ∈ List.take 32 _ from mem)
        rcases List.mem_append.mp hm with newFact | oldFact
        · cases hk : kind == .var || p.live c with
          | false => simp [info, hk] at newFact
          | true =>
            cases shapeEq : ex.shape with
            | none => simp [info, hk, shapeEq] at newFact
            | some shape =>
              simp only [info, hk, ite_true, shapeEq, Option.map_some, Option.toList_some,
                List.mem_singleton, Prod.mk.injEq] at newFact
              obtain ⟨rfl, rfl⟩ := newFact
              have present : (mid.cellAt d).isSome := by
                change (kind == .var || p.live d) = true at hk
                simp only [Bool.or_eq_true] at hk
                rcases hk with hv | live
                · exact varExists (beq_iff_eq.mp hv)
                · obtain ⟨b, hb⟩ := Option.isSome_iff_exists.mp (hs.live d live)
                  simp [ext.cells _ _ hb]
              obtain ⟨b, hb⟩ := Option.isSome_iff_exists.mp present
              exact ⟨v, b.mutable, by simp [State.cellAt_assign_self, hb], sh.transport (described sh shapeEq) (fun _ _ h => h)⟩
        · obtain ⟨old, different⟩ := List.mem_filter.mp oldFact
          obtain ⟨val, m, binding, witness⟩ := hs.entry _ _ old
          exact ⟨val, m, by simpa only [State.cellAt_assign_other _ _ _ _ (bne_iff_ne.mp different)] using ext.cells _ _ binding,
            sh.transport witness (fun a o ho => ext.objs a o ho)⟩⟩

private def step (p : Facts) : (u : LinkedUnit) → Step p u
  | ⟨id, .decl kind x e, env⟩ =>
    match h : env.vars.lookup x, expr? p env e with
    | some c, some ex => declStep p id env kind x e c h ex
    | _, _ => ⟨_, empty, fun _ _ _ => rfl, fun _ _ _ _ _ _ => empty_holds _⟩
  | ⟨_, .expr e, env⟩ =>
    match expr? p env e with
    | some ex => ⟨.expr e, p, fun _ _ _ => rfl, by
        intro w n s t f hs run
        cases n with
        | zero => cases run
        | succ n =>
          obtain ⟨v, mid, he, hf⟩ := bind_ok run
          cases hf
          have ext := (ex.result n s t v hs he).1
          constructor
          · intro a ha
            obtain ⟨b, hb⟩ := Option.isSome_iff_exists.mp (hs.live _ ha)
            simp [ext.cells _ _ hb]
          · intro a sh ha
            obtain ⟨v, m, hb, hv⟩ := hs.entry _ _ ha
            exact ⟨v, m, ext.cells _ _ hb, sh.transport hv ext.objs⟩⟩
    | none => ⟨_, empty, fun _ _ _ => rfl, fun _ _ _ _ _ _ => empty_holds _⟩
  | u => ⟨u.stmt, empty, fun _ _ _ => rfl, fun _ _ _ _ _ _ => empty_holds _⟩

private def normalize (p : Facts) (units : List LinkedUnit) :
    (result : List LinkedUnit) × PLift (∀ {w} n (s final : State w), Holds p s →
      (runUnits n units s = .ok () final ↔ runUnits n result s = .ok () final)) :=
  match units with
  | [] => ⟨[], ⟨fun _ _ _ _ => Iff.rfl⟩⟩
  | u :: rest =>
    let a := step p u
    let ⟨tail, ⟨ih⟩⟩ := normalize a.after rest
    ⟨(u.id, a.stmt, u.env) :: tail, ⟨by
      intro w n s final hs
      obtain ⟨id, st, env⟩ := u
      have eqv : runUnit (W := w) n env id st s = runUnit n env id a.stmt s := by
        simp only [runUnit, inRegion, M.bind_apply, modifyState, a.same n _ (hs.region _)]
      simp only [LinkedUnit.id, LinkedUnit.env, runUnits, M.bind_apply, ← eqv]
      cases h : runUnit n env id st s with
      | ok f t =>
        obtain ⟨mid, exec, rfl⟩ := runUnit_ok h
        exact ih n _ final ((a.post n _ _ f (hs.region _) exec).region 0)
      | _ => rfl⟩⟩

/-- This proof concerns the actual linked units. It starts with no object
facts. The probe only supplies domains of cells allocated by linking. -/
def prepare (ready : State World.probe) (units : List LinkedUnit) :
    (result : List LinkedUnit) × PLift (∀ w n (final : State w),
      runUnits n units (LinkedBehavior.linkState w ready) = .ok () final ↔
      runUnits n result (LinkedBehavior.linkState w ready) = .ok () final) :=
  let p : Facts := ⟨fun c => (ready.cellAt c).isSome, []⟩
  let ⟨result, ⟨proof⟩⟩ := normalize p units
  ⟨result, ⟨fun w n final => proof n _ final ⟨fun _ h => h, by simp [p]⟩⟩⟩

end CoreJS.PrefixProjection
