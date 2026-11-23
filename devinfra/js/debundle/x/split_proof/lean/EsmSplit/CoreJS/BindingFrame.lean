import EsmSplit.CoreJS.RequestFrame

/-!
# Reachability frames for lexical scopes

A new cell belongs to the running unit's region. The caller supplies a
capability envelope that admits fresh cells there and contains any edges
in their initial values. The same envelope then contains the new scope
and any sub-environment a closure captures.
-/

namespace CoreJS

variable {W : World}

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

end CoreJS
