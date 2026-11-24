import EsmSplit.CoreJS.Request.Frame

/-!
# Reachability frames for lexical scopes

A new cell belongs to the running unit's region. The caller supplies a
capability envelope that admits fresh cells there and contains any edges
in their initial values. The same envelope then contains the new scope
and any sub-environment a closure captures.
-/

namespace CoreJS

variable {W : World} {α : Type}

theorem reachFrame_newCell {r R cells A} (away : Away r R A)
    (outside : ∀ a, a.region ≠ r → A (.cell a)) (b : Binding) (safe : ∀ n ∈ b.nodes, A n) :
    ReachFrame r R cells A (fun a => A (.cell a)) (newCell b : M W Addr) := by
  constructor
  · intro s H C _ hr
    simp only [newCell, State.put]
    have hc : (s.cells.set! r C)[s.region]? = s.cells[s.region]? := by simp [Ne.symm hr]
    rw [hc]
    cases s.cells[s.region]? <;> simp only [Res.put, State.put, set!_comm _ (Ne.symm hr)]
  · intro s _ hr
    unfold newCell
    split
    · rename_i bs hb
      refine ⟨rfl, by simp [hr], ?_, rfl⟩
      intro a ha
      rw [cellAt_push hb]
      split
      · rename_i eq
        exact False.elim (away.reads a (outside a (by rw [eq]; exact hr)) ha)
      · rfl
    · exact Preserves.refl r R s
  · intro s hs hr
    unfold newCell
    split
    · rename_i bs hb
      refine ⟨outside _ hr, hs.handed, hs.supplied, ?_⟩
      intro source target sourceAllowed edge
      cases edge with
      | obj ho hn => exact hs.closed _ _ sourceAllowed (.obj ho hn)
      | @cell a old target hc hn =>
        rw [cellAt_push hb] at hc
        split at hc
        · cases hc; exact safe target hn
        · exact hs.closed _ _ sourceAllowed (.cell hc hn)
    · exact hs

theorem reachFrame_newCells {r R cells A} (away : Away r R A)
    (outside : ∀ a, a.region ≠ r → A (.cell a)) (bs : List (Name × Binding))
    (safe : ∀ p ∈ bs, ∀ n ∈ p.2.nodes, A n) :
    ReachFrame r R cells A (fun frame => ∀ p ∈ frame, A (.cell p.2)) (newCells bs : M W _) := by
  induction bs with
  | nil => exact reachFrame_pure r R cells A [] (by simp)
  | cons head tail ih =>
    obtain ⟨x, b⟩ := head
    simp only [newCells]
    apply (reachFrame_newCell away outside b (safe _ List.mem_cons_self)).bind
    intro a ha
    apply (ih fun p hp => safe p (List.mem_cons_of_mem _ hp)).bind
    intro frame hf
    apply reachFrame_pure r R cells A
    intro p hp
    rcases List.mem_cons.mp hp with rfl | hp
    · exact ha
    · exact hf p hp

theorem reachFrame_declare {r R cells A} (away : Away r R A)
    (outside : ∀ a, a.region ≠ r → A (.cell a)) (env : Env) (bs : List (Name × Binding))
    (henv : ∀ n ∈ env.nodes, A n) (safe : ∀ p ∈ bs, ∀ n ∈ p.2.nodes, A n) :
    ReachFrame r R cells A (fun env' => ∀ n ∈ env'.nodes, A n) (declare env bs : M W Env) := by
  unfold declare
  apply (reachFrame_newCells away outside bs safe).bind
  intro frame hf
  apply reachFrame_pure r R cells A
  intro n hn
  simp only [Env.nodes, List.map_append, List.mem_append, List.mem_map] at hn
  rcases hn with ((⟨p, hp, rfl⟩ | ⟨p, hp, rfl⟩) | hn)
  · exact hf p hp
  · exact henv _ (List.mem_append_left _ (List.mem_map.mpr ⟨p, hp, rfl⟩))
  · exact henv _ (List.mem_append_right _ hn)


theorem Env.nodes_lookup {A : HeapNode → Prop} {env : Env} (safe : ∀ n ∈ env.nodes, A n) {name : Name} {a : Addr}
    (ha : env.vars.lookup name = some a) : A (.cell a) :=
  safe _ (List.mem_append_left _ (List.mem_map.mpr ⟨(name, a), lookup_mem ha, rfl⟩))

theorem Env.nodes_capture {A : HeapNode → Prop} {env : Env} (safe : ∀ n ∈ env.nodes, A n) (names : List Name) :
    ∀ n ∈ (env.capture names).nodes, A n := by
  intro node h
  rcases List.mem_append.mp h with cells | this
  · obtain ⟨p, hp, rfl⟩ := List.mem_map.mp cells
    obtain ⟨name, _, mapped⟩ := List.mem_filterMap.mp hp
    cases found : env.vars.lookup name with
    | none => simp [found] at mapped
    | some addr =>
      simp only [found, Option.map_some, Option.some.injEq] at mapped
      subst p
      exact Env.nodes_lookup safe found
  · exact safe _ (List.mem_append_right _ this)

theorem Env.nodes_this {A : HeapNode → Prop} {env : Env} (safe : ∀ n ∈ env.nodes, A n) : SafeVal A env.this :=
  fun node hn => safe node (List.mem_append_right _ hn)

theorem Env.nodes_setThis {A : HeapNode → Prop} {env : Env} (safe : ∀ n ∈ env.nodes, A n) {this : Val}
    (hs : SafeVal A this) : ∀ n ∈ ({ env with this } : Env).nodes, A n := by
  intro node h
  rcases List.mem_append.mp h with cells | recv
  · exact safe node (List.mem_append_left _ cells)
  · exact hs node recv

theorem reachFrame_selfScope {r R cells A} (away : Away r R A)
    (outside : ∀ a, a.region ≠ r → A (.cell a)) (env : Env) (self : Bool) (name : Name)
    (safe : ∀ node ∈ env.nodes, A node) :
    ReachFrame r R cells A (fun e => ∀ node ∈ e.nodes, A node) (selfScope env self name : M W Env) := by
  cases self
  · exact reachFrame_pure r R cells A env safe
  · apply reachFrame_declare away outside env _ safe
    intro p hp n hn
    rcases List.mem_singleton.mp hp with rfl
    cases hn

theorem reachFrame_initVar {r R cells A} (away : Away r R A) (env : Env) (name : Name) (v : Val)
    (safe : ∀ node ∈ env.nodes, A node) (hv : SafeVal A v) :
    ReachFrame r R cells A (fun _ => True) (initVar env name v : M W Unit) := by
  unfold initVar
  split
  · rename_i address resolved
    exact reachFrame_setCell away _ (Env.nodes_lookup safe resolved) v hv
  · exact reachFrame_unknown r R cells A _ _

theorem reachFrame_initSelf {r R cells A} (away : Away r R A) (env : Env) (self : Bool) (name : Name) (v : Val)
    (safe : ∀ node ∈ env.nodes, A node) (hv : SafeVal A v) :
    ReachFrame r R cells A (fun _ => True) (initSelf env self name v : M W Unit) := by
  cases self
  · exact reachFrame_pure r R cells A () trivial
  · exact reachFrame_initVar away env name v safe hv

theorem reachFrame_errorValue {r R cells A P} (v : Val) (safe : SafeVal A v) :
    ReachFrame r R cells A P (throwVal v : M W α) :=
  ReachFrame.of_frames (frames_throwVal r R v) (fun _ hs _ => ⟨safe, hs⟩)

theorem reachFrame_typeError {r R cells A P} (why : String) :
    ReachFrame r R cells A P (throwErr .type why : M W α) :=
  reachFrame_errorValue (.error .type why) (fun _ hn => by cases hn)

theorem reachFrame_fuel {r R cells A P} :
    ReachFrame r R cells A P (outOfFuel : M W α) :=
  ReachFrame.of_frames (frames_outOfFuel r R) fun _ _ _ => trivial

private theorem ReachFrame.bind_stateRead {β : Type} {r R cells A} {P : β → Prop} {Q : α → Prop}
    {read : State W → β} {next : β → M W α}
    (before : ReachFrame r R cells A P (do pure (read (← getState)) : M W β))
    (after : ∀ b, P b → ReachFrame r R cells A Q (next b)) :
    ReachFrame r R cells A Q (do next (read (← getState))) := by
  simpa only [M.bind_assoc, M.pure_bind] using before.bind after

theorem reachFrame_cellAt {r R cells A} (away : Away r R A) (a : Addr) (accessible : A (.cell a)) :
    ReachFrame r R cells A (fun value => ∀ b ∈ value, ∀ n ∈ b.nodes, A n)
      (do pure ((← getState).cellAt a) : M W (Option Binding)) := by
  constructor
  · intro s H C _ _
    have outside : a.region ≠ r := away.region _ accessible
    have same : (s.put r H C).cellAt a = s.cellAt a := by rw [State.cellAt_put, ite_eq_right outside]
    simp only [M.bind_apply, getState]
    rw [same]
    rfl
  · intro s _ _
    exact Preserves.refl r R s
  · intro s hs _
    refine ⟨?_, hs⟩
    intro b hb n hn
    exact hs.closed _ _ accessible (.cell hb hn)

theorem reachFrame_readVar {r R cells A} (away : Away r R A) (env : Env) (x : Name)
    (safe : ∀ node ∈ env.nodes, A node) :
    ReachFrame r R cells A (SafeVal A) (readVar env x : M W Val) := by
  have reference why : ReachFrame r R cells A (SafeVal A) (throwErr .reference why : M W Val) :=
    reachFrame_errorValue (.error .reference why) (fun _ hn => by cases hn)
  unfold readVar
  cases found : env.vars.lookup x with
  | none => exact reference _
  | some a =>
    simp only []
    let next : Option Binding → M W Val
      | some { value := some v, .. } => pure v
      | _ => throwErr .reference s!"cannot access '{x}' before initialization"
    apply (reachFrame_cellAt away a (Env.nodes_lookup safe found)).bind_stateRead (next := next)
    intro b hb
    cases b with
    | none => exact reference _
    | some b =>
      rcases b with ⟨value, mutable⟩
      cases value with
      | none => exact reference _
      | some v =>
        exact reachFrame_pure r R cells A v (fun n hn => hb _ rfl n (by simpa [Binding.nodes] using hn))

theorem reachFrame_writeVar {r R cells A} (away : Away r R A) (env : Env) (x : Name) (v : Val)
    (safe : ∀ node ∈ env.nodes, A node) (hv : SafeVal A v) :
    ReachFrame r R cells A (fun _ => True) (writeVar env x v : M W Unit) := by
  have reference why : ReachFrame r R cells A (fun _ => True) (throwErr .reference why : M W Unit) :=
    reachFrame_errorValue (.error .reference why) (fun _ hn => by cases hn)
  unfold writeVar
  cases found : env.vars.lookup x with
  | none => exact reference _
  | some a =>
    have ha := Env.nodes_lookup safe found
    simp only []
    let next : Option Binding → M W Unit
      | some { value := some _, mutable := true } => setCell a v
      | some { value := some _, mutable := false } => throwErr .type "assignment to constant variable"
      | _ => throwErr .reference s!"cannot access '{x}' before initialization"
    apply (reachFrame_cellAt away a ha).bind_stateRead (next := next)
    intro b _
    cases b with
    | none => exact reference _
    | some old =>
      rcases old with ⟨value, mutable⟩
      cases value with
      | none => exact reference _
      | some v' =>
        cases mutable
        · exact reachFrame_typeError _
        · exact reachFrame_setCell away a ha v hv

private theorem reachFrame_boundCells {r R cells A} (away : Away r R A) (env : Env) (xs : List Name)
    (safe : ∀ node ∈ env.nodes, A node) :
    ReachFrame r R cells A (fun bs => ∀ p ∈ bs, ∀ n ∈ p.2.nodes, A n)
      (do pure (boundCells (← getState) env xs) : M W (List (Name × Binding))) := by
  constructor
  · intro s H C _ _
    have same : boundCells (s.put r H C) env xs = boundCells s env xs := by
      unfold boundCells
      congr 1
      funext name
      cases found : env.vars.lookup name with
      | none => rfl
      | some a =>
        have outside : a.region ≠ r := away.region _ (Env.nodes_lookup safe found)
        let rest (b : Binding) : Option (Name × Binding) := some (name, b)
        change ((s.put r H C).cellAt a).bind rest = (s.cellAt a).bind rest
        rw [State.cellAt_put, ite_eq_right outside]
    simp only [M.bind_apply, getState]
    rw [same]
    rfl
  · intro s _ _
    exact Preserves.refl r R s
  · intro s hs _
    refine ⟨?_, hs⟩
    intro p hp node hn
    obtain ⟨name, _, hp⟩ := List.mem_filterMap.mp hp
    cases he : env.vars.lookup name with
    | none => simp [he] at hp
    | some a =>
      cases hc : s.cellAt a with
      | none => simp [he, hc] at hp
      | some b =>
        have eq : (name, b) = p := by simpa [he, hc] using hp
        subst p
        exact hs.closed _ _ (Env.nodes_lookup safe he) (.cell hc hn)

theorem reachFrame_copyBindings {r R cells A} (away : Away r R A)
    (outside : ∀ a, a.region ≠ r → A (.cell a)) (env : Env) (xs : List Name)
    (safe : ∀ n ∈ env.nodes, A n) :
    ReachFrame r R cells A (fun env' => ∀ n ∈ env'.nodes, A n) (copyBindings env xs : M W Env) :=
  (reachFrame_boundCells away env xs safe).bind_stateRead fun bs hb => reachFrame_declare away outside env bs safe hb

theorem reachFrame_addPrototype {r R cells A} {generator : Bool} (away : Away r R A)
    (root : A (.obj (if generator then generatorProto else objectProto)))
    (allocate : ∀ o : Obj, (∀ node ∈ o.nodes, A node) →
      ReachFrame r R cells A (fun a => A (.obj a)) (alloc o : M W Addr))
    (a : Addr) (ha : A (.obj a)) :
    ReachFrame r R cells A (fun _ => True) (addPrototype a generator : M W Unit) := by
  unfold addPrototype
  apply (allocate _ ?_).bind
  · intro p hp
    apply reachFrame_modifyObj away a ha
    intro o node hn
    simp [Obj.nodes, Property.nodes, Val.nodes] at hn ⊢
    rcases hn with h | h | rfl | h
    · exact Or.inl (Or.inl h)
    · exact Or.inl (Or.inr (Or.inl h))
    · exact Or.inr hp
    · exact Or.inl (Or.inr (Or.inr h))
  · intro node hn
    cases generator
    · simp [Obj.nodes, Property.nodes, Val.nodes] at hn
      rcases hn with rfl | rfl
      · exact root
      · exact ha
    · simp [Obj.nodes, Property.nodes, Val.nodes] at hn
      subst node
      exact root

theorem reachFrame_makeFunc {r R cells A} (away : Away r R A)
    (outside : ∀ a, a.region ≠ r → A (.cell a))
    (prototypeRoot : ∀ generator : Bool, A (.obj (if generator then generatorProto else objectProto)))
    (functionRoot : ∀ f : Func, A (.obj f.intrinsicProto))
    (allocate : ∀ o : Obj, (∀ node ∈ o.nodes, A node) →
      ReachFrame r R cells A (fun a => A (.obj a)) (alloc o : M W Addr))
    (env : Env) (safe : ∀ node ∈ env.nodes, A node) (f : Func) :
    ReachFrame r R cells A (SafeVal A) (makeFunc env f : M W Val) := by
  unfold makeFunc
  apply (reachFrame_selfScope away outside env f.self f.name safe).bind
  intro e he
  apply (allocate _ ?_).bind
  · intro a ha
    have proto : ReachFrame r R cells A (fun _ => True)
        (if f.kind == .normal || f.kind == .generator then addPrototype a (f.kind == .generator)
         else pure () : M W Unit) := by
      split
      · exact reachFrame_addPrototype away (prototypeRoot _) allocate a ha
      · exact reachFrame_pure r R cells A () trivial
    apply proto.bind
    intro _ _
    exact (reachFrame_initSelf away e f.self f.name (.obj a) he (safeVal_obj.2 ha)).bind fun _ _ =>
      reachFrame_pure r R cells A (Val.obj a) (safeVal_obj.2 ha)
  · intro node hn
    simp [Obj.nodes, Property.nodes, Val.nodes, Code.nodes] at hn
    rcases hn with rfl | captures
    · exact functionRoot f
    · exact Env.nodes_capture he f.closes node captures

private theorem ReachFrame.forM {r R cells A} {α : Type} {f : α → M W PUnit}
    (xs : List α) (step : ∀ x ∈ xs, ReachFrame r R cells A (fun _ => True) (f x)) :
    ReachFrame r R cells A (fun _ => True) (xs.forM f) := by
  induction xs with
  | nil => exact reachFrame_pure r R cells A () trivial
  | cons x xs ih =>
    exact (step x List.mem_cons_self).bind fun _ _ => ih fun x hx => step x (List.mem_cons_of_mem _ hx)

theorem reachFrame_enterBlock {r R cells A} (away : Away r R A)
    (outside : ∀ a, a.region ≠ r → A (.cell a))
    (prototypeRoot : ∀ generator : Bool, A (.obj (if generator then generatorProto else objectProto)))
    (functionRoot : ∀ f : Func, A (.obj f.intrinsicProto))
    (allocate : ∀ o : Obj, (∀ node ∈ o.nodes, A node) →
      ReachFrame r R cells A (fun a => A (.obj a)) (alloc o : M W Addr))
    (env : Env) (safe : ∀ node ∈ env.nodes, A node) (body : List Stmt) :
    ReachFrame r R cells A (fun env' => ∀ node ∈ env'.nodes, A node) (enterBlock env body : M W Env) := by
  unfold enterBlock
  apply (reachFrame_declare away outside env _ safe ?_).bind
  · intro e he
    apply (ReachFrame.forM (funDecls body) ?_).bind
    · intro _ _
      exact reachFrame_pure r R cells A e he
    · intro f _
      apply (reachFrame_makeFunc away outside prototypeRoot functionRoot allocate e he f).bind
      intro v hv
      exact reachFrame_initVar away e f.name v he hv
  · intro p hp node hn
    obtain ⟨⟨name, mutable⟩, _, rfl⟩ := List.mem_map.mp hp
    cases hn

theorem reachFrame_enterFunction {r R cells A} (away : Away r R A)
    (outside : ∀ a, a.region ≠ r → A (.cell a))
    (prototypeRoot : ∀ generator : Bool, A (.obj (if generator then generatorProto else objectProto)))
    (functionRoot : ∀ f : Func, A (.obj f.intrinsicProto))
    (allocate : ∀ o : Obj, (∀ node ∈ o.nodes, A node) →
      ReachFrame r R cells A (fun a => A (.obj a)) (alloc o : M W Addr))
    (f : Func) (closed : Env) (safe : ∀ node ∈ closed.nodes, A node) (this : Val) (ht : SafeVal A this)
    (args : List Val) (ha : ∀ arg ∈ args, SafeVal A arg) :
    ReachFrame r R cells A (fun env' => ∀ node ∈ env'.nodes, A node) (enterFunction f closed this args : M W Env) := by
  unfold enterFunction
  apply (reachFrame_declare away outside _ _ (Env.nodes_setThis safe ?_) ?_).bind
  · intro e he
    exact reachFrame_enterBlock away outside prototypeRoot functionRoot allocate e he f.body
  · split
    · exact Env.nodes_this safe
    · exact ht
  · intro p hp node hn
    rcases List.mem_append.mp hp with params | vars
    · obtain ⟨⟨_, i⟩, _, rfl⟩ := List.mem_map.mp params
      have arg : SafeVal A (args.getD i .undef) := by
        rw [List.getD_eq_getElem?_getD]
        cases h : args[i]? with
        | none => exact fun _ hn => by cases hn
        | some v => exact ha v (list_index_mem h)
      exact arg node (by simpa [Binding.nodes] using hn)
    · obtain ⟨_, _, rfl⟩ := List.mem_map.mp vars
      cases hn

end CoreJS
