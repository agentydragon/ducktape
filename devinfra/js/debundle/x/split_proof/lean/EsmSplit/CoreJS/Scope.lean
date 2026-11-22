import EsmSplit.CoreJS.Frame

/-!
# CoreJS: code sees a scope only through the names that are free in it

`Same`: two scopes that give the free names of a piece of code the same bindings, and the same `this`, run it the
same way: the same result in the same state, for every bound on the depth and every world. The states are equal, not
related, because a closure keeps only the bindings of the names its code can mean (`Env.capture`).

This is what lets a statement move to another module: the module's scope has to agree with the old one on the names
the statement mentions, and on nothing else.
-/

namespace CoreJS

variable {W : World} {α β γ : Type}

/-! ## Sequences -/

theorem M.bind_apply (m : M W α) (f : α → M W β) (s : State W) :
    (m >>= f) s = match m s with
      | .ok a s' => f a s'
      | .throw v s' => .throw v s'
      | .unknown why s' => .unknown why s'
      | .fuel => .fuel := rfl

theorem M.pure_bind (a : α) (f : α → M W β) : (pure a >>= f) = f a := rfl

theorem M.bind_assoc (m : M W α) (f : α → M W β) (g : β → M W γ) : m >>= f >>= g = m >>= fun a => f a >>= g := by
  funext s
  simp only [M.bind_apply]
  cases m s <;> rfl

theorem M.bind_congr {m m' : M W α} {f f' : α → M W β} (hm : m = m') (hf : ∀ a, f a = f' a) :
    m >>= f = m' >>= f' := by
  subst hm
  exact congrArg _ (funext hf)

/-- The rest of a sequence has to agree only on what the first part can give. -/
theorem M.bind_congr_ok {m : M W α} {f f' : α → M W β} (hf : ∀ s a s', m s = .ok a s' → f a s' = f' a s') :
    m >>= f = m >>= f' := by
  funext s
  simp only [M.bind_apply]
  cases h : m s with
  | ok a s' => exact hf s a s' h
  | _ => rfl

theorem M.map_congr {m m' : M W α} (f : α → β) (hm : m = m') : f <$> m = f <$> m' := by rw [hm]

theorem forM_congr {f g : α → M W PUnit} : ∀ (l : List α), (∀ a ∈ l, f a = g a) → l.forM f = l.forM g
  | [], _ => rfl
  | a :: l, h => by
    show (f a >>= fun _ => l.forM f) = (g a >>= fun _ => l.forM g)
    exact M.bind_congr (h a (List.mem_cons_self ..)) fun _ => forM_congr l fun b hb => h b (List.mem_cons_of_mem _ hb)

theorem filterMap_congr {f g : α → Option β} : ∀ (l : List α), (∀ a ∈ l, f a = g a) → l.filterMap f = l.filterMap g
  | [], _ => rfl
  | a :: l, h => by
    rw [List.filterMap_cons, List.filterMap_cons, h a (List.mem_cons_self ..),
      filterMap_congr l fun b hb => h b (List.mem_cons_of_mem _ hb)]

/-! ## Scopes that agree on some names -/

/-- `env` and `env'` have the same `this` and give every name of `N` the same binding, or none. -/
structure Agree (N : List Name) (env env' : Env) : Prop where
  this : env.this = env'.this
  vars : ∀ x ∈ N, env.vars.lookup x = env'.vars.lookup x

theorem Agree.mono {N N' : List Name} {env env' : Env} (h : Agree N env env') (sub : ∀ x ∈ N', x ∈ N) :
    Agree N' env env' :=
  ⟨h.this, fun x hx => h.vars x (sub x hx)⟩

/-- A new innermost frame, the same in both scopes: they then also agree on the names of the frame. -/
theorem Agree.push {N N' : List Name} {env env' : Env} (h : Agree N env env') (frame : List (Name × Addr))
    (sub : ∀ x ∈ N', x ∈ N ∨ x ∈ frame.map (·.1)) :
    Agree N' { env with vars := frame ++ env.vars } { env' with vars := frame ++ env'.vars } := by
  refine ⟨h.this, fun x hx => ?_⟩
  simp only [List.lookup_append]
  cases hf : frame.lookup x with
  | some a => rfl
  | none =>
    rcases sub x hx with hN | hframe
    · simp [h.vars x hN]
    · obtain ⟨p, hp, rfl⟩ := List.mem_map.1 hframe
      have := List.lookup_eq_none_iff.1 hf p hp
      simp at this

theorem Agree.withThis {N : List Name} {env env' : Env} (h : Agree N env env') (v : Val) :
    Agree N { env with this := v } { env' with this := v } :=
  ⟨rfl, h.vars⟩

theorem capture_agree {N : List Name} {env env' : Env} (h : Agree N env env') : env.capture N = env'.capture N := by
  unfold Env.capture
  rw [h.this, filterMap_congr _ fun x hx => by rw [h.vars x (List.mem_eraseDups.1 hx)]]

theorem readVar_agree {env env' : Env} {x : Name} (h : env.vars.lookup x = env'.vars.lookup x) :
    (readVar env x : M W Val) = readVar env' x := by
  unfold readVar
  rw [h]

theorem writeVar_agree {env env' : Env} {x : Name} (h : env.vars.lookup x = env'.vars.lookup x) (v : Val) :
    (writeVar env x v : M W Unit) = writeVar env' x v := by
  unfold writeVar
  rw [h]

theorem initVar_agree {env env' : Env} {x : Name} (h : env.vars.lookup x = env'.vars.lookup x) (v : Val) :
    (initVar env x v : M W Unit) = initVar env' x v := by
  unfold initVar
  rw [h]

/-- The names an assignment looks up. -/
def Ref.names : Ref → List Name
  | .name x | .frozen x => [x]
  | .prop .. => []

theorem getRef_agree (call : Call W) {env env' : Env} {r : Ref} (h : Agree r.names env env') :
    getRef call env r = getRef call env' r := by
  cases r with
  | name x => exact readVar_agree (h.vars x (by simp [Ref.names]))
  | frozen x => exact readVar_agree (h.vars x (by simp [Ref.names]))
  | prop base key => rfl

theorem putRef_agree (call : Call W) {env env' : Env} {r : Ref} (h : Agree r.names env env') (v : Val) :
    putRef call env r v = putRef call env' r v := by
  cases r with
  | name x => exact writeVar_agree (h.vars x (by simp [Ref.names])) v
  | frozen x => rfl
  | prop base key => rfl

/-- What an assignment target evaluates to names only names of the target. -/
theorem evalTarget_names {n : Nat} {env : Env} {t : Target} {s s' : State W} {r : Ref}
    (h : evalTarget n env t s = .ok r s') : ∀ x ∈ r.names, x ∈ t.free := by
  cases n with
  | zero => unfold evalTarget at h; cases h
  | succ n =>
    cases t with
    | var x => unfold evalTarget at h; cases h; simp [Ref.names, Target.free]
    | frozen x => unfold evalTarget at h; cases h; simp [Ref.names, Target.free]
    | member o k =>
      unfold evalTarget at h
      obtain ⟨base, t1, _, h⟩ := bind_ok h
      obtain ⟨key, t2, _, h⟩ := bind_ok h
      cases h
      simp [Ref.names]

/-! ## Entering a scope -/

theorem newCells_names : ∀ (bs : List (Name × Binding)) {s s' : State W} {frame : List (Name × Addr)},
    newCells bs s = .ok frame s' → frame.map (·.1) = bs.map (·.1)
  | [], s, s', frame, h => by unfold newCells at h; cases h; rfl
  | (x, b) :: rest, s, s', frame, h => by
    unfold newCells at h
    obtain ⟨a, t1, _, h⟩ := bind_ok h
    obtain ⟨tail, t2, htail, h⟩ := bind_ok h
    cases h
    simp [newCells_names rest htail]

/-- What follows new bindings has to agree only for frames with the names that were declared. -/
theorem newCells_congr {bs : List (Name × Binding)} {k k' : List (Name × Addr) → M W β}
    (h : ∀ frame, frame.map (·.1) = bs.map (·.1) → k frame = k' frame) : newCells bs >>= k = newCells bs >>= k' :=
  M.bind_congr_ok fun _ frame _ hs => by rw [h frame (newCells_names bs hs)]

theorem declare_bind (env : Env) (bs : List (Name × Binding)) (k : Env → M W β) :
    declare env bs >>= k = newCells bs >>= fun frame => k { env with vars := frame ++ env.vars } := by
  unfold declare
  rw [M.bind_assoc]
  rfl

/-- A scope entered from two scopes that agree on `N`: what follows may assume they agree on `N'`, the names of `N`
and the declared ones. -/
theorem declare_congr {N N' : List Name} {env env' : Env} (bs : List (Name × Binding)) {k k' : Env → M W β}
    (h : Agree N env env') (sub : ∀ x ∈ N', x ∈ N ∨ x ∈ bs.map (·.1))
    (hk : ∀ env1 env1', Agree N' env1 env1' → k env1 = k' env1') : declare env bs >>= k = declare env' bs >>= k' := by
  rw [declare_bind, declare_bind]
  exact newCells_congr fun frame hframe => hk _ _ (h.push frame fun x hx => hframe ▸ sub x hx)

theorem selfScope_congr {N N' : List Name} {env env' : Env} (self : Bool) (name : Name) {k k' : Env → M W β}
    (h : Agree N env env') (sub : ∀ x ∈ N', x ∈ N ∨ (self = true ∧ x = name))
    (hk : ∀ env1 env1', Agree N' env1 env1' → k env1 = k' env1') :
    selfScope env self name >>= k = selfScope env' self name >>= k' := by
  unfold selfScope
  cases self with
  | false =>
    exact hk _ _ (h.mono fun x hx => (sub x hx).elim id fun h' => by simp at h')
  | true =>
    refine declare_congr _ h (fun x hx => ?_) hk
    rcases sub x hx with hN | ⟨_, rfl⟩
    · exact Or.inl hN
    · exact Or.inr (by simp)

theorem initSelf_agree {env env' : Env} (self : Bool) {name : Name} (v : Val)
    (h : self = true → env.vars.lookup name = env'.vars.lookup name) :
    (initSelf env self name v : M W Unit) = initSelf env' self name v := by
  unfold initSelf
  cases self with
  | false => rfl
  | true => exact initVar_agree (h rfl) v

/-- A function made in two scopes that agree on its free names is the same object. -/
theorem makeFunc_agree {env env' : Env} {f : Func} (h : Agree f.free env env') :
    (makeFunc env f : M W Val) = makeFunc env' f := by
  unfold makeFunc
  refine selfScope_congr (N' := f.closes) f.self f.name h (fun x hx => ?_) fun env1 env1' h1 => ?_
  · unfold Func.closes at hx
    cases hs : f.self with
    | false => simp [hs] at hx; exact Or.inl hx
    | true =>
      simp only [hs, ite_true, List.mem_cons] at hx
      exact hx.elim (fun e => Or.inr ⟨rfl, e⟩) Or.inl
  · rw [capture_agree h1]
    refine M.bind_congr rfl fun a => M.bind_congr rfl fun _ => M.bind_congr ?_ fun _ => rfl
    exact initSelf_agree f.self (.obj a) fun hs => h1.vars f.name (by simp [Func.closes, hs])

theorem boundCells_agree (s : State W) {env env' : Env} {names : List Name}
    (h : ∀ x ∈ names, env.vars.lookup x = env'.vars.lookup x) : boundCells s env names = boundCells s env' names := by
  unfold boundCells
  exact filterMap_congr _ fun x hx => by rw [h x hx]

theorem copyBindings_congr {N : List Name} {env env' : Env} (names : List Name) {k k' : Env → M W β}
    (h : Agree N env env') (hnames : ∀ x ∈ names, x ∈ N) (hk : ∀ env1 env1', Agree N env1 env1' → k env1 = k' env1') :
    copyBindings env names >>= k = copyBindings env' names >>= k' := by
  unfold copyBindings
  rw [M.bind_assoc, M.bind_assoc]
  refine M.bind_congr rfl fun s => ?_
  rw [boundCells_agree s fun x hx => h.vars x (hnames x hx)]
  exact declare_congr _ h (fun x hx => Or.inl hx) hk

/-! ## The names of the parts of a statement list -/

theorem free_mem_freeStmts {s : Stmt} : ∀ {ss : List Stmt}, s ∈ ss → ∀ x ∈ s.free, x ∈ freeStmts ss
  | t :: ss, hs, x, hx => by
    unfold freeStmts
    rcases List.mem_cons.1 hs with rfl | hs
    · exact List.mem_append_left _ hx
    · exact List.mem_append_right _ (free_mem_freeStmts hs x hx)

theorem funDecl_of_mem {f : Func} {ss : List Stmt} (h : f ∈ funDecls ss) : Stmt.funDecl f ∈ ss := by
  unfold funDecls at h
  obtain ⟨s, hs, hf⟩ := List.mem_filterMap.1 h
  cases s <;> simp at hf
  subst hf
  exact hs

theorem funDecl_name_lex {f : Func} {ss : List Stmt} (h : Stmt.funDecl f ∈ ss) : f.name ∈ lexNames ss := by
  unfold lexNames lexDecls
  exact List.mem_map.2 ⟨(f.name, true), List.mem_filterMap.2 ⟨_, h, rfl⟩, rfl⟩

theorem lexBindings_names (ss : List Stmt) :
    ((lexDecls ss).map fun (x, mutable) => (x, ({ value := none, mutable } : Binding))).map (·.1) = lexNames ss := by
  simp [lexNames, List.map_map, Function.comp_def]

/-- A block entered from two scopes that agree on `N`: its functions are the same objects, and what follows may
assume the scopes agree on `N'`, names of `N` and names the block declares. -/
theorem enterBlock_congr {N N' : List Name} {env env' : Env} (ss : List Stmt) {k k' : Env → M W β}
    (h : Agree N env env') (sub : ∀ x ∈ N', x ∈ N ∨ x ∈ lexNames ss)
    (hfuns : ∀ f ∈ funDecls ss, ∀ x ∈ f.free, x ∈ N') (hk : ∀ env1 env1', Agree N' env1 env1' → k env1 = k' env1') :
    enterBlock env ss >>= k = enterBlock env' ss >>= k' := by
  unfold enterBlock
  rw [M.bind_assoc, M.bind_assoc]
  refine declare_congr (N' := N' ++ lexNames ss) _ h (fun x hx => ?_) fun env1 env1' h1 => ?_
  · rw [lexBindings_names]
    rcases List.mem_append.1 hx with h' | h'
    · exact sub x h'
    · exact Or.inr h'
  · rw [M.bind_assoc, M.bind_assoc]
    refine M.bind_congr (forM_congr _ fun f hf => ?_) fun _ => ?_
    · exact M.bind_congr (makeFunc_agree (h1.mono fun x hx => List.mem_append_left _ (hfuns f hf x hx))) fun v =>
        initVar_agree (h1.vars _ (List.mem_append_right _ (funDecl_name_lex (funDecl_of_mem hf)))) v
    · exact hk _ _ (h1.mono fun x hx => List.mem_append_left _ hx)

/-- An opaque statement that only makes functions does the same in two scopes that agree on the names it declares. -/
theorem initClosures_agree (code : Nat) (cells : List (Option Addr)) {env env' : Env} :
    ∀ (decls : List (Name × DeclKind)) (fns : List (String × Nat × Bool)) (part : Nat),
      (∀ x ∈ decls.map (·.1), env.vars.lookup x = env'.vars.lookup x) →
      (initClosures code cells env decls fns part : M W Unit) = initClosures code cells env' decls fns part
  | [], [], _, _ => rfl
  | [], _ :: _, _, _ => rfl
  | _ :: _, [], _, _ => rfl
  | (x, kind) :: decls, (name, length, constructor) :: fns, part, h => by
    unfold initClosures
    refine M.bind_congr rfl fun v => M.bind_congr ?_ fun _ =>
      initClosures_agree code cells decls fns (part + 1) fun y hy => h y (List.mem_cons_of_mem _ hy)
    split
    · exact writeVar_agree (h x List.mem_cons_self) v
    · exact initVar_agree (h x List.mem_cons_self) v

/-- An opaque statement is handed the same bindings in two scopes that agree on its names. -/
theorem opaqueCells_agree {decls : List (Name × DeclKind)} {funs : List (Name × Nat × Bool)} {free : List Name}
    {env env' : Env} (h : Agree (opaqueNames decls funs free) env env') :
    opaqueCells env decls funs free = opaqueCells env' decls funs free := by
  unfold opaqueCells
  exact List.map_congr_left fun x hx => h.vars x hx

/-! ## The interpreter, rule by rule -/

/-- Every function of the interpreter that takes a scope gives the same computation in two scopes that agree on the
free names of the code, with the bound `n` on the depth. -/
structure Same (W : World) (n : Nat) : Prop where
  expr : ∀ env env' e, Agree e.free env env' → (evalExpr n env e : M W _) = evalExpr n env' e
  args : ∀ env env' es, Agree (freeExprs es) env env' → (evalArgs n env es : M W _) = evalArgs n env' es
  target : ∀ env env' t, Agree t.free env env' → (evalTarget n env t : M W _) = evalTarget n env' t
  props : ∀ env env' a ps, Agree (freeProps ps) env env' → (evalProps n env a ps : M W _) = evalProps n env' a ps
  members : ∀ env env' proto c ms, Agree (freeMembers ms) env env' →
    (evalMembers n env proto c ms : M W _) = evalMembers n env' proto c ms
  fields : ∀ env env' a fs, Agree (fs.flatMap (·.2.free)) env env' →
    (evalFields n env a fs : M W _) = evalFields n env' a fs
  stmt : ∀ env env' s, Agree s.free env env' → (execStmt n env s : M W _) = execStmt n env' s
  stmts : ∀ env env' ss, Agree (freeStmts ss) env env' → (execStmts n env ss : M W _) = execStmts n env' ss
  loop : ∀ env env' fresh test update body, Agree (fresh ++ (test.free ++ (update.free ++ body.free))) env env' →
    (loop n env fresh test update body : M W _) = loop n env' fresh test update body

/-- `x` is among the free names of the whole, given that it is among those of a part (`hx`). -/
syntax "free_mem" : tactic
macro_rules
  | `(tactic| free_mem) => `(tactic|
    (intro x hx
     first
     | (simp only [Expr.free, Target.free, PropDef.free, ClassMember.free, Stmt.free, freeExprs, freeProps,
          freeMembers, freeStmts, List.flatMap_cons, List.mem_append, List.mem_cons, List.not_mem_nil, or_false] at hx
        done)
     | (try simp only [Expr.free, Target.free, PropDef.free, ClassMember.free, Stmt.free, freeExprs, freeProps,
          freeMembers, freeStmts, List.flatMap_cons, List.mem_append, List.mem_cons, List.not_mem_nil,
          or_false] at hx
        try simp only [Expr.free, Target.free, PropDef.free, ClassMember.free, Stmt.free, freeExprs, freeProps,
          freeMembers, freeStmts, List.flatMap_cons, List.mem_append, List.mem_cons, List.not_mem_nil, or_false]
        first | exact hx | simp only [hx, true_or, or_true])))

/-- This name is among the free names of the code that mentions it. -/
syntax "free_here" : tactic
macro_rules
  | `(tactic| free_here) => `(tactic| simp [Expr.free, Target.free, Stmt.free])

/-- A part of a rule: one of the interpreter's functions in the two scopes, or an operation that looks a name up. -/
syntax "same_part " ident ident : tactic
macro_rules
  | `(tactic| same_part $ih $hN) => `(tactic| first
    | with_reducible exact ($ih).expr _ _ _ (($hN).mono (by free_mem))
    | with_reducible exact ($ih).args _ _ _ (($hN).mono (by free_mem))
    | with_reducible exact ($ih).target _ _ _ (($hN).mono (by free_mem))
    | with_reducible exact ($ih).props _ _ _ _ (($hN).mono (by free_mem))
    | with_reducible exact ($ih).members _ _ _ _ _ (($hN).mono (by free_mem))
    | with_reducible exact ($ih).fields _ _ _ _ (($hN).mono (by free_mem))
    | with_reducible exact ($ih).stmt _ _ _ (($hN).mono (by free_mem))
    | with_reducible exact ($ih).stmts _ _ _ (($hN).mono (by free_mem))
    | with_reducible exact makeFunc_agree (($hN).mono (by free_mem))
    | with_reducible exact readVar_agree (($hN).vars _ (by free_here))
    | with_reducible exact writeVar_agree (($hN).vars _ (by free_here)) _
    | with_reducible exact initVar_agree (($hN).vars _ (by free_here)) _
    | with_reducible exact getRef_agree _ (by assumption)
    | with_reducible exact putRef_agree _ (by assumption) _)

/-- One step of a comparison: the same computation on both sides, a sequence, or a case split. -/
syntax "same_step" : tactic
macro_rules
  | `(tactic| same_step) => `(tactic| first
    | with_reducible rfl
    | contradiction
    | with_reducible refine M.bind_congr ?_ (fun _ => ?_)
    | with_reducible refine M.map_congr _ ?_
    | split)

syntax "same " ident ident : tactic
macro_rules
  | `(tactic| same $ih $hN) => `(tactic| repeat' first | same_part $ih $hN | same_step)

theorem fresh_lex (ss : List Stmt) :
    ∀ x ∈ (lexDecls ss).filterMap (fun (x, mutable) => if mutable then some x else none), x ∈ lexNames ss := by
  intro x hx
  obtain ⟨⟨y, m⟩, hp, h⟩ := List.mem_filterMap.1 hx
  cases m <;> simp at h
  subst h
  exact List.mem_map.2 ⟨_, hp, rfl⟩

theorem fieldInits_free : ∀ (ms : List ClassMember), ∀ init ∈ fieldInits ms, ∀ x ∈ init.free, x ∈ freeMembers ms
  | m :: ms, init, hinit, x, hx => by
    unfold freeMembers
    cases m with
    | field static k e =>
      unfold fieldInits at hinit
      rcases List.mem_cons.1 hinit with rfl | hinit
      · exact List.mem_append_left _ (by simp [ClassMember.free, hx])
      · exact List.mem_append_right _ (fieldInits_free ms init hinit x hx)
    | method | getter | setter =>
      unfold fieldInits at hinit
      exact List.mem_append_right _ (fieldInits_free ms init hinit x hx)

/-- `typeof` of a name: a name that does not resolve gives "undefined". -/
theorem evalExpr_typeof_var (n : Nat) (env : Env) (x : Name) :
    (evalExpr (n + 1) env (.unary .typeof (.var x)) : M W Val) =
      if (env.vars.lookup x).isNone then pure (.str "undefined")
      else readVar env x >>= fun v => getState >>= fun s => lift (unop s .typeof v) := by
  simp only [evalExpr]

section step
variable {n : Nat} (ih : Same W n)
include ih

/-- An assignment: the target first, then the rest, which looks up only the name the target gave. -/
theorem Same.assign_like {env env' : Env} {t : Target} {N : List Name} (hN : Agree N env env')
    (ht : ∀ x ∈ t.free, x ∈ N) {k k' : Ref → M W β}
    (hk : ∀ r, Agree r.names env env' → k r = k' r) :
    (evalTarget n env t >>= k : M W β) = evalTarget n env' t >>= k' := by
  rw [ih.target env env' t (hN.mono ht)]
  exact M.bind_congr_ok fun s r s' hr => by rw [hk r (hN.mono fun x hx => ht x (evalTarget_names hr x hx))]

theorem Same.cls_succ (env env' : Env) (name : String) (self : Bool) (ctor : Func) (members : List ClassMember)
    (hN : Agree (Expr.cls name self ctor members).free env env') :
    (evalExpr (n + 1) env (.cls name self ctor members) : M W _) = evalExpr (n + 1) env' (.cls name self ctor members) := by
  unfold evalExpr
  refine selfScope_congr (N' := (if self then [name] else []) ++ (ctor.free ++ freeMembers members)) self name hN
    (fun x hx => ?_) fun env1 env1' h1 => ?_
  · rcases List.mem_append.1 hx with hx | hx
    · cases self <;> simp at hx
      exact Or.inr ⟨rfl, hx⟩
    · by_cases hc : (self && x == name) = true
      · simp only [Bool.and_eq_true, beq_iff_eq] at hc
        exact Or.inr hc
      · exact Or.inl (by
          simp only [Expr.free, List.mem_filter]
          exact ⟨hx, by cases h : (self && x == name) <;> simp_all⟩)
  · have h2 : Agree (ctor.free ++ freeMembers members) env1 env1' := h1.mono fun x hx => List.mem_append_right _ hx
    dsimp only
    rw [capture_agree h2]
    refine M.bind_congr rfl fun proto => M.bind_congr rfl fun c => M.bind_congr rfl fun _ => ?_
    refine M.bind_congr (ih.members _ _ _ _ _ (h2.mono fun x hx => List.mem_append_right _ hx)) fun keys => ?_
    refine M.bind_congr rfl fun _ => M.bind_congr ?_ fun _ => M.bind_congr ?_ fun _ => rfl
    · exact initSelf_agree self (.obj c) fun hs => h1.vars name (by simp [hs])
    · refine ih.fields _ _ _ _ ((h2.withThis (.obj c)).mono fun x hx => ?_)
      obtain ⟨⟨k, init⟩, hmem, hx⟩ := List.mem_flatMap.1 hx
      obtain ⟨⟨⟨static, k'⟩, init'⟩, hzip, hsome⟩ := List.mem_filterMap.1 hmem
      cases static <;> simp at hsome
      obtain ⟨rfl, rfl⟩ := hsome
      exact List.mem_append_right _ (fieldInits_free members _ (List.of_mem_zip hzip).2 x hx)

theorem Same.derived_succ (env env' : Env) (name : String) (self : Bool) (parent : Expr) (members : List ClassMember)
    (hN : Agree (Expr.derived name self parent members).free env env') :
    (evalExpr (n + 1) env (.derived name self parent members) : M W _) =
      evalExpr (n + 1) env' (.derived name self parent members) := by
  simp only [evalExpr]
  split
  · rfl
  · refine selfScope_congr (N' := (if self then [name] else []) ++ (parent.free ++ freeMembers members)) self name hN
      (fun x hx => ?_) fun env1 env1' h1 => ?_
    · rcases List.mem_append.1 hx with hx | hx
      · cases self <;> simp at hx
        exact Or.inr ⟨rfl, hx⟩
      · by_cases hc : (self && x == name) = true
        · simp only [Bool.and_eq_true, beq_iff_eq] at hc
          exact Or.inr hc
        · exact Or.inl (by
            simp only [Expr.free, List.mem_filter]
            exact ⟨hx, by cases h : (self && x == name) <;> simp_all⟩)
    · have h2 : Agree (parent.free ++ freeMembers members) env1 env1' := h1.mono fun x hx => List.mem_append_right _ hx
      refine M.bind_congr (ih.expr _ _ _ (h2.mono fun x hx => List.mem_append_left _ hx)) fun base => ?_
      refine M.bind_congr rfl fun parents => ?_
      unfold finishDerived
      refine M.bind_congr rfl fun proto => M.bind_congr rfl fun c => M.bind_congr rfl fun _ => ?_
      refine M.bind_congr (ih.members _ _ _ _ _ (h2.mono fun x hx => List.mem_append_right _ hx)) fun _ => ?_
      refine M.bind_congr ?_ fun _ => rfl
      exact initSelf_agree self (.obj c) fun hs => h1.vars name (by simp [hs])

set_option maxHeartbeats 1000000 in
theorem Same.expr_succ (env env' : Env) (e : Expr) (hN : Agree e.free env env') :
    (evalExpr (n + 1) env e : M W _) = evalExpr (n + 1) env' e := by
  cases e with
  | this => unfold evalExpr; rw [hN.this]
  | assign t rhs =>
    unfold evalExpr
    refine ih.assign_like hN (by free_mem) fun r hr => ?_
    same ih hN
  | assignOp t op rhs =>
    unfold evalExpr
    refine ih.assign_like hN (by free_mem) fun r hr => ?_
    same ih hN
  | assignLogical t op rhs =>
    unfold evalExpr
    refine ih.assign_like hN (by free_mem) fun r hr => ?_
    same ih hN
  | update t inc pre =>
    unfold evalExpr
    refine ih.assign_like hN (by free_mem) fun r hr => ?_
    same ih hN
  | cls name self ctor members => exact ih.cls_succ env env' name self ctor members hN
  | derived name self parent members => exact ih.derived_succ env env' name self parent members hN
  | unary op a =>
    cases op with
    | typeof =>
      cases a with
      | var x =>
        rw [evalExpr_typeof_var, evalExpr_typeof_var, hN.vars x (by free_here),
          readVar_agree (hN.vars x (by free_here))]
      | _ => unfold evalExpr; same ih hN
    | _ => unfold evalExpr; same ih hN
  | _ => unfold evalExpr; same ih hN

theorem Same.args_succ (env env' : Env) (es : List Expr) (hN : Agree (freeExprs es) env env') :
    (evalArgs (n + 1) env es : M W _) = evalArgs (n + 1) env' es := by
  cases es <;> unfold evalArgs <;> same ih hN

theorem Same.target_succ (env env' : Env) (t : Target) (hN : Agree t.free env env') :
    (evalTarget (n + 1) env t : M W _) = evalTarget (n + 1) env' t := by
  cases t <;> unfold evalTarget <;> same ih hN

theorem Same.props_succ (env env' : Env) (a : Addr) (ps : List PropDef) (hN : Agree (freeProps ps) env env') :
    (evalProps (n + 1) env a ps : M W _) = evalProps (n + 1) env' a ps := by
  cases ps <;> unfold evalProps <;> same ih hN

theorem Same.members_succ (env env' : Env) (proto c : Addr) (ms : List ClassMember)
    (hN : Agree (freeMembers ms) env env') :
    (evalMembers (n + 1) env proto c ms : M W _) = evalMembers (n + 1) env' proto c ms := by
  cases ms <;> unfold evalMembers <;> same ih hN

theorem Same.fields_succ (env env' : Env) (a : Addr) (fs : List (String × Expr))
    (hN : Agree (fs.flatMap (·.2.free)) env env') :
    (evalFields (n + 1) env a fs : M W _) = evalFields (n + 1) env' a fs := by
  cases fs <;> unfold evalFields <;> same ih hN

theorem Same.stmts_succ (env env' : Env) (ss : List Stmt) (hN : Agree (freeStmts ss) env env') :
    (execStmts (n + 1) env ss : M W _) = execStmts (n + 1) env' ss := by
  cases ss <;> unfold execStmts <;> same ih hN

end step

section step
variable {n : Nat} (ih : Same W n)
include ih

theorem Same.stmt_succ (env env' : Env) (s : Stmt) (hN : Agree s.free env env') :
    (execStmt (n + 1) env s : M W _) = execStmt (n + 1) env' s := by
  cases s with
  | decl kind x e => cases kind <;> unfold execStmt <;> same ih hN
  | block ss =>
    unfold execStmt
    refine enterBlock_congr (N' := freeStmts ss) ss hN (fun x hx => ?_) (fun f hf x hx => ?_)
      fun env1 env1' h1 => ih.stmts _ _ _ h1
    · by_cases hc : (lexNames ss).contains x = true
      · exact Or.inr (List.contains_iff_mem.1 hc)
      · exact Or.inl (by simp only [Stmt.free, List.mem_filter]; exact ⟨hx, by simpa using hc⟩)
    · exact free_mem_freeStmts (funDecl_of_mem hf) x (by simp [Stmt.free, hx])
  | loop init test update body =>
    unfold execStmt
    refine enterBlock_congr (N' := (init.free ++ (test.free ++ (update.free ++ body.free))) ++ lexNames [init]) [init]
      hN (fun x hx => ?_) (fun f hf x hx => ?_) fun env1 env1' h1 => ?_
    · rcases List.mem_append.1 hx with hx | hx
      · by_cases hc : (lexNames [init]).contains x = true
        · exact Or.inr (List.contains_iff_mem.1 hc)
        · exact Or.inl (by simp only [Stmt.free, List.mem_filter]; exact ⟨hx, by simpa using hc⟩)
      · exact Or.inr hx
    · have := free_mem_freeStmts (funDecl_of_mem hf) x (by simp [Stmt.free, hx])
      simp only [freeStmts, List.append_nil] at this
      exact List.mem_append_left _ (List.mem_append_left _ this)
    · refine M.bind_congr (ih.stmt _ _ _ (h1.mono fun x hx => by simp [hx])) fun _ => ?_
      dsimp only
      refine copyBindings_congr _ h1 (fun x hx => List.mem_append_right _ (fresh_lex [init] x hx))
        fun env2 env2' h2 => ih.loop _ _ _ _ _ _ (h2.mono fun x hx => ?_)
      rcases List.mem_append.1 hx with hx | hx
      · exact List.mem_append_right _ (fresh_lex [init] x hx)
      · exact List.mem_append_left _ (List.mem_append_right _ hx)
  | tryCatch body param handler =>
    unfold execStmt
    dsimp only
    rw [ih.stmt env env' body (hN.mono (by free_mem))]
    funext s
    cases execStmt n env' body s with
    | throw v s' =>
      dsimp only
      refine congrFun (?_ : _ = _) s'
      cases param with
      | none => exact ih.stmt _ _ _ (hN.mono fun x hx => by simp [Stmt.free, hx])
      | some p =>
        refine declare_congr (N' := handler.free) _ hN (fun x hx => ?_) fun env1 env1' h1 => ih.stmt _ _ _ h1
        by_cases hp : x = p
        · exact Or.inr (by simp [hp])
        · exact Or.inl (by
            simp only [Stmt.free, List.mem_append, List.mem_filter]
            refine Or.inr ⟨hx, ?_⟩
            rw [Bool.not_eq_true']
            exact beq_eq_false_iff_ne.2 fun h => hp (Option.some.inj h).symm)
    | _ => rfl
  | tryFinally body finalizer =>
    unfold execStmt
    dsimp only
    rw [ih.stmt env env' body (hN.mono (by free_mem)), ih.stmt env env' finalizer (hN.mono (by free_mem))]
  | «opaque» code decls funs free run =>
    cases run with
    | world =>
      unfold execStmt
      dsimp only
      rw [hN.this, opaqueCells_agree hN]
    | closures fns =>
      unfold execStmt
      dsimp only
      rw [opaqueCells_agree hN, initClosures_agree code _ decls fns _ fun x hx =>
        hN.vars x (by simp only [Stmt.free, opaqueNames]; exact List.mem_append_left _ hx)]
  | _ => unfold execStmt; same ih hN

theorem Same.loop_succ (env env' : Env) (fresh : List Name) (test update : Expr) (body : Stmt)
    (hN : Agree (fresh ++ (test.free ++ (update.free ++ body.free))) env env') :
    (CoreJS.loop (n + 1) env fresh test update body : M W _) = CoreJS.loop (n + 1) env' fresh test update body := by
  unfold CoreJS.loop
  refine M.bind_congr (ih.expr _ _ _ (hN.mono fun x hx => by simp [hx])) fun v => M.bind_congr rfl fun b => ?_
  split
  · rfl
  · refine M.bind_congr (ih.stmt _ _ _ (hN.mono fun x hx => by simp [hx])) fun flow => ?_
    split <;> first
      | rfl
      | exact copyBindings_congr fresh hN (fun x hx => by simp [hx]) fun env1 env1' h1 =>
          M.bind_congr (ih.expr _ _ _ (h1.mono fun x hx => by simp [hx])) fun _ => ih.loop _ _ _ _ _ _ h1

end step

/-- **Code sees a scope only through its free names.** Two scopes that agree on the free names of a piece of code,
and on `this`, run it the same way: the same result in the same state, for every bound and every world. -/
theorem same : ∀ n, Same W n
  | 0 => ⟨fun _ _ _ _ => rfl, fun _ _ _ _ => rfl, fun _ _ _ _ => rfl, fun _ _ _ _ _ => rfl, fun _ _ _ _ _ _ => rfl,
      fun _ _ _ _ _ => rfl, fun _ _ _ _ => rfl, fun _ _ _ _ => rfl, fun _ _ _ _ _ _ _ => rfl⟩
  | n + 1 =>
    have ih := same n
    ⟨ih.expr_succ, ih.args_succ, ih.target_succ, ih.props_succ, ih.members_succ, ih.fields_succ, ih.stmt_succ,
      ih.stmts_succ, ih.loop_succ⟩

/-- A statement of a program, run in the scopes of two modules that agree on the names it mentions. -/
theorem runUnit_agree {env env' : Env} {s : Stmt} (h : Agree s.free env env') (fuel id : Nat) :
    (runUnit fuel env id s : M W Flow) = runUnit fuel env' id s := by
  unfold runUnit
  rw [(same fuel).stmt env env' s h]

theorem lookup_filterMap (f : Name → Option Addr) (x : Name) : ∀ (l : List Name),
    (l.filterMap fun y => (f y).map (y, ·)).lookup x = if x ∈ l then f x else none
  | [] => rfl
  | y :: l => by
    have ih := lookup_filterMap f x l
    by_cases hxy : x = y
    · subst hxy
      cases hf : f x with
      | none =>
        simp only [List.filterMap_cons, hf, Option.map_none, List.mem_cons, true_or, ite_true]
        rw [ih]
        split <;> simp [hf]
      | some a => simp [hf]
    · have hne : (x == y) = false := by simpa using hxy
      cases hf : f y with
      | none => simp [hf, ih, hxy]
      | some a => simp [hf, List.lookup_cons, hne, ih, hxy]

/-- What a closure keeps is enough: code runs in the bindings a closure kept of a scope as it runs in the scope,
when the kept names include its free names. -/
theorem capture_faithful (n : Nat) (env : Env) (s : Stmt) (N : List Name) (hN : ∀ x ∈ s.free, x ∈ N) :
    (execStmt n (env.capture N) s : M W Flow) = execStmt n env s := by
  refine (same n).stmt _ _ s ⟨rfl, fun x hx => ?_⟩
  show (N.eraseDups.filterMap fun y => (env.vars.lookup y).map (y, ·)).lookup x = _
  simp only [lookup_filterMap, List.mem_eraseDups, hN x hx, ite_true]

#print axioms same
#print axioms runUnit_agree
#print axioms capture_faithful

end CoreJS
