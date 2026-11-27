import EsmSplit.CoreJS.HostFrame

/-!
# Reachability of property requests

The lookup budget counts every object in the heap. Replacing a protected
region can change that count, even when the receiver cannot reach the region.
A successful search has a first hit along a path of distinct objects, so
the object count of either heap is enough for the same path.
The same path proof bounds the cycle check before changing a prototype.
Property writes preserve the envelope when both the target and inserted
values are accessible. Property deletion only removes existing edges.
-/

namespace CoreJS

variable {W : World}

variable {α : Type}

private def findOnPrototype (s : State W) (test : Addr → Obj → Option α) : Nat → Addr → Option α
  | 0, _ => none
  | steps + 1, a =>
    match s.objAt a with
    | none => none
    | some o =>
      match test a o with
      | some result => some result
      | none => o.proto.bind (findOnPrototype s test steps)

private inductive PrototypePath (s : State W) (test : Addr → Obj → Option α) (p : α) : Addr → List Addr → Prop
  | own {a o} : s.objAt a = some o → test a o = some p → PrototypePath s test p a [a]
  | inherited {a o parent rest} : s.objAt a = some o → test a o = none →
      o.proto = some parent → PrototypePath s test p parent rest → PrototypePath s test p a (a :: rest)

private theorem findOnPrototype_path {s : State W} {test : Addr → Obj → Option α} {p} {budget : Nat} {a : Addr}
    (h : findOnPrototype s test budget a = some p) :
    ∃ path, PrototypePath s test p a path ∧ path.length ≤ budget := by
  induction budget generalizing a with
  | zero => simp [findOnPrototype] at h
  | succ n ih =>
    cases ho : s.objAt a with
    | none => simp [findOnPrototype, ho] at h
    | some o =>
      cases hk : test a o with
      | some q =>
        simp only [findOnPrototype, ho, hk, Option.some.injEq] at h
        subst q
        exact ⟨[a], .own ho hk, by simp⟩
      | none =>
        cases hp : o.proto with
        | none => simp [findOnPrototype, ho, hk, hp] at h
        | some parent =>
          have ht : findOnPrototype s test n parent = some p := by simpa [findOnPrototype, ho, hk, hp] using h
          obtain ⟨rest, ht, hn⟩ := ih ht
          exact ⟨a :: rest, .inherited ho hk hp ht, by simpa using Nat.succ_le_succ hn⟩

private theorem PrototypePath.found {s : State W} {test : Addr → Obj → Option α} {p a path} (hp : PrototypePath s test p a path)
    {budget : Nat} (hbudget : path.length ≤ budget) : findOnPrototype s test budget a = some p := by
  induction hp generalizing budget with
  | own ho hk =>
    cases budget with
    | zero => simp at hbudget
    | succ n => simp [findOnPrototype, ho, hk]
  | inherited ho hk hproto _ ih =>
    cases budget with
    | zero => simp at hbudget
    | succ n =>
      simp only [findOnPrototype, ho, hk, hproto, Option.bind_some]
      exact ih (by simpa using hbudget)

private theorem PrototypePath.beforeHit {s : State W} {test : Addr → Obj → Option α} {p a path} (hp : PrototypePath s test p a path)
    {budget : Nat} (hbudget : budget < path.length) : findOnPrototype s test budget a = none := by
  induction hp generalizing budget with
  | own ho hk =>
    cases budget with
    | zero => rfl
    | succ n => simp at hbudget
  | inherited ho hk hproto _ ih =>
    cases budget with
    | zero => rfl
    | succ n =>
      simp only [findOnPrototype, ho, hk, hproto, Option.bind_some]
      exact ih (by simpa using hbudget)

private theorem PrototypePath.suffix {s : State W} {test : Addr → Obj → Option α} {p a path} (hp : PrototypePath s test p a path)
    {b : Addr} (hb : b ∈ path) :
    ∃ rest, PrototypePath s test p b rest ∧ rest.length ≤ path.length := by
  induction hp with
  | @own addr o ho hk =>
    simp only [List.mem_singleton] at hb
    subst b
    exact ⟨[addr], .own ho hk, Nat.le_refl _⟩
  | @inherited addr o parent rest ho hk hproto ht ih =>
    rcases List.mem_cons.mp hb with rfl | hb
    · exact ⟨b :: rest, .inherited ho hk hproto ht, Nat.le_refl _⟩
    · obtain ⟨suffix, hp, hlen⟩ := ih hb
      exact ⟨suffix, hp, Nat.le_trans hlen (Nat.le_succ _)⟩

private theorem PrototypePath.nodup {s : State W} {test : Addr → Obj → Option α} {p a path}
    (hp : PrototypePath s test p a path) : path.Nodup := by
  induction hp with
  | own => simp
  | @inherited addr o parent rest ho hk hproto ht ih =>
    refine List.nodup_cons.mpr ⟨fun h => ?_, ih⟩
    obtain ⟨suffix, hf, hlen⟩ := ht.suffix h
    have never := (PrototypePath.inherited ho hk hproto ht).beforeHit
      (budget := suffix.length) (by simpa using Nat.lt_succ_of_le hlen)
    rw [hf.found (Nat.le_refl _)] at never
    contradiction

private theorem PrototypePath.existsObject {s : State W} {test : Addr → Obj → Option α} {p a path}
    (hp : PrototypePath s test p a path) : ∀ b ∈ path, ∃ o, s.objAt b = some o := by
  induction hp with
  | own ho _ => simpa using (show ∃ o, s.objAt _ = some o from ⟨_, ho⟩)
  | inherited ho _ _ _ ih =>
    intro b hb
    rcases List.mem_cons.mp hb with rfl | hb
    · exact ⟨_, ho⟩
    · exact ih b hb

private def objectAddresses : List (Array Obj) → Nat → List Addr
  | [], _ => []
  | region :: rest, i => (List.range region.size).map (Addr.mk i) ++ objectAddresses rest (i + 1)

private theorem objectAddresses_length (regions : List (Array Obj)) (base acc : Nat) :
    (objectAddresses regions base).length + acc = regions.foldl (fun n objects => n + objects.size) acc := by
  induction regions generalizing base acc with
  | nil => simp [objectAddresses]
  | cons region rest ih =>
    simp only [objectAddresses, List.length_append, List.length_map, List.length_range, List.foldl_cons]
    rw [← ih (base + 1) (acc + region.size)]
    omega

private theorem objectAddresses_mem {regions : List (Array Obj)} (base region index : Nat) {o : Obj}
    (h : regions[region]?.bind (·[index]?) = some o) :
    ⟨base + region, index⟩ ∈ objectAddresses regions base := by
  induction regions generalizing base region with
  | nil => simp at h
  | cons objects rest ih =>
    cases region with
    | zero =>
      simp only [List.getElem?_cons_zero, Option.bind_some] at h
      have hi := (Array.getElem?_eq_some_iff.mp h).1
      apply List.mem_append_left
      simp [List.mem_map, hi]
    | succ r =>
      simp only [List.getElem?_cons_succ] at h
      exact List.mem_append_right _
        (by simpa [Nat.add_assoc, Nat.add_comm 1 r] using ih (base + 1) r h)

private theorem PrototypePath.withinCount {s : State W} {test : Addr → Obj → Option α} {p a path}
    (hp : PrototypePath s test p a path) : path.length ≤ s.objectCount := by
  have bound := hp.nodup.length_le_of_subset (l₂ := objectAddresses s.heap.toList 0) (by
    intro b hb
    obtain ⟨_, h⟩ := hp.existsObject b hb
    unfold State.objAt at h
    simpa using objectAddresses_mem (regions := s.heap.toList) 0 b.region b.index (by simpa using h))
  rw [show (objectAddresses s.heap.toList 0).length = s.objectCount by
    simpa [State.objectCount, Array.foldl_toList] using objectAddresses_length s.heap.toList 0 0] at bound
  exact bound

private theorem Access.prototype {cells A} {s : State W} (hs : Access cells A s)
    {a o parent} (ha : A (.obj a)) (ho : s.objAt a = some o) (hp : o.proto = some parent) :
    A (.obj parent) :=
  hs.closed _ _ ha (.obj ho (by simp [Obj.nodes, hp]))

private theorem PrototypePath.transfer {cells A} {s t : State W} (hs : Access cells A s)
    (same : ∀ a, A (.obj a) → s.objAt a = t.objAt a)
    {test : Addr → Obj → Option α} {p a path} (hp : PrototypePath s test p a path) (ha : A (.obj a)) :
    PrototypePath t test p a path := by
  induction hp with
  | own ho hk => exact .own ((same _ ha).symm.trans ho) hk
  | inherited ho hk hproto _ ih =>
    exact .inherited ((same _ ha).symm.trans ho) hk hproto (ih (hs.prototype ha ho hproto))

private theorem findProp_eq_findOnPrototype (s : State W) (n : Nat) (a : Addr) (key : PropertyKey) :
    findProp s n a key = findOnPrototype s (fun _ o => o.props.lookup key) n a := by
  induction n generalizing a with
  | zero => rfl
  | succ n ih =>
    cases ho : s.objAt a with
    | none => simp [findProp, findOnPrototype, ho]
    | some o =>
      cases h : o.props.lookup key <;> simp only [findProp, findOnPrototype, ho, h]
      cases o.proto <;> simp [ih]

private theorem PrototypePath.propertySafe {cells A} {s : State W} (hs : Access cells A s)
    {key p a path} (hp : PrototypePath s (fun _ o => o.props.lookup key) p a path) (ha : A (.obj a)) :
    ∀ node ∈ p.nodes, A node := by
  induction hp with
  | own ho hk =>
    intro node hn
    apply hs.closed _ _ ha
    apply Edge.obj ho
    apply List.mem_append_left
    apply List.mem_append_left
    apply List.mem_append_right
    exact List.mem_flatMap.mpr ⟨(key, p), lookup_mem hk, hn⟩
  | inherited ho _ hproto _ ih => exact ih (hs.prototype ha ho hproto)

private theorem findOnPrototype_same {cells A} {s t : State W} (hs : Access cells A s) (ht : Access cells A t)
    (same : ∀ a, A (.obj a) → s.objAt a = t.objAt a) {a} (test : Addr → Obj → Option α) (ha : A (.obj a)) :
    findOnPrototype s test s.objectCount a = findOnPrototype t test t.objectCount a := by
  cases h : findOnPrototype s test s.objectCount a with
  | some p =>
    obtain ⟨_, path, _⟩ := findOnPrototype_path h
    have target := path.transfer hs same ha
    exact (target.found target.withinCount).symm
  | none =>
    cases h' : findOnPrototype t test t.objectCount a with
    | none => rfl
    | some p =>
      obtain ⟨_, path, _⟩ := findOnPrototype_path h'
      have target := path.transfer ht (fun addr accessible => (same addr accessible).symm) ha
      have present := target.found target.withinCount
      rw [h] at present
      contradiction

private theorem findProp_same {cells A} {s t : State W} (hs : Access cells A s) (ht : Access cells A t)
    (same : ∀ a, A (.obj a) → s.objAt a = t.objAt a) {a key} (ha : A (.obj a)) :
    findProp s s.objectCount a key = findProp t t.objectCount a key := by
  rw [findProp_eq_findOnPrototype, findProp_eq_findOnPrototype]
  exact findOnPrototype_same hs ht same (fun _ o => o.props.lookup key) ha

/-- Prototype lookup stays within the capability envelope, including when a
replacement changes the total heap size used as the walk's budget. -/
theorem reachFrame_lookupProp {r R cells A} (away : Away r R A) {a : Addr} (ha : A (.obj a)) (key : PropertyKey) :
    ReachFrame r R cells A (fun result => ∀ p, result = some p → ∀ node ∈ p.nodes, A node)
      (lookupProp a key : M W (Option Property)) := by
  constructor
  · intro s H C hs _
    have same (b : Addr) (hb : A (.obj b)) : s.objAt b = (s.put r H C).objAt b := by
      have excluded : b.region ≠ r := away.region _ hb
      rw [State.objAt_put, ite_eq_right excluded]
    let t := s.put r H C
    change Res.ok (findProp t t.objectCount a key) t = .ok (findProp s s.objectCount a key) t
    rw [findProp_same hs (hs.put away H C) same ha]
  · intro s _ _
    exact Preserves.refl r R s
  · intro s hs _
    refine ⟨fun p hp => ?_, hs⟩
    rw [findProp_eq_findOnPrototype] at hp
    obtain ⟨_, path, _⟩ := findOnPrototype_path hp
    exact path.propertySafe hs ha

theorem reachFrame_throwErr {α : Type} (r R cells A) (P : α → Prop) (kind : ErrorKind) (message : String) :
    ReachFrame r R cells A P (throwErr kind message : M W α) :=
  ⟨fun _ _ _ _ _ => rfl, fun s _ _ => Preserves.refl r R s,
    fun _ hs _ => ⟨by simp [SafeVal, Val.nodes], hs⟩⟩

theorem reachFrame_toKey (r R cells A) (v : Val) :
    ReachFrame r R cells A (fun _ => True) (toKey v : M W PropertyKey) :=
  reachFrame_lift _ (fun _ _ => trivial)

theorem reachFrame_hasProp {r R cells A} (away : Away r R A) (key base : Val) (hb : SafeVal A base) :
    ReachFrame r R cells A (SafeVal A) (hasProp key base : M W Val) := by
  cases base <;> simp only [hasProp] <;>
    try first
    | exact reachFrame_throwErr r R cells A _ _ _
    | exact reachFrame_unknown r R cells A _ _
  case obj a =>
    exact (reachFrame_toKey r R cells A key).bind fun k _ =>
      (reachFrame_lookupProp away (safeVal_obj.1 hb) k).bind fun found _ =>
        reachFrame_pure r R cells A (P := SafeVal A) (Val.bool found.isSome) (by simp [SafeVal, Val.nodes])

/-- Test an inherited or own property without exposing the protected region. -/
theorem perform_has {r R cells A} (away : Away r R A) (call : Call W)
    (construct : Val → List Val → M W Val) (code : Nat) (key base : WVal) :
    ReachFrame r R cells A (fun _ => True) (perform call construct code cells (.has key base)) :=
  (reachFrame_fromW r R cells A key).bind fun k _ =>
    (reachFrame_fromW r R cells A base).bind fun o ho =>
      (reachFrame_hasProp away k o ho).bind fun result safe =>
        (reachFrame_toW r R cells A result safe).bind fun w _ => reachFrame_pure r R cells A (Told.value w) trivial

/-- A property read either returns a reachable data value or invokes a reachable getter. -/
theorem reachFrame_getFrom {r R cells A} (away : Away r R A) (call : Call W)
    (framed : ∀ f base, SafeVal A f → SafeVal A base →
      ReachFrame r R cells A (SafeVal A) (call f base []))
    {a} (ha : A (.obj a)) (base : Val) (hb : SafeVal A base) (key : PropertyKey) :
    ReachFrame r R cells A (SafeVal A) (getFrom call a base key) := by
  unfold getFrom
  apply (reachFrame_lookupProp away ha key).bind
  intro result safe
  cases result with
  | none => exact reachFrame_pure r R cells A (P := SafeVal A) Val.undef (by simp [SafeVal, Val.nodes])
  | some prop =>
    have hp := safe prop rfl
    cases prop with
    | data v => exact reachFrame_pure r R cells A v hp
    | accessor getter setter =>
      have hg : SafeVal A getter := fun node hn => hp node (List.mem_append_left _ hn)
      cases getter
      case undef => exact reachFrame_pure r R cells A (P := SafeVal A) Val.undef (by simp [SafeVal, Val.nodes])
      all_goals exact framed _ _ hg hb

private theorem reachFrame_protoProp {r R cells A} (away : Away r R A) (call : Call W)
    (framed : ∀ f base, SafeVal A f → SafeVal A base →
      ReachFrame r R cells A (SafeVal A) (call f base []))
    (root : A (.obj objectProto)) (owner : String) (names : List String)
    (base : Val) (hb : SafeVal A base) (key : PropertyKey) :
    ReachFrame r R cells A (SafeVal A) (protoProp call owner names base key) := by
  cases key <;> simp only [protoProp] <;> try split
  all_goals first
  | exact reachFrame_pure r R cells A _ (by simp [SafeVal, Val.nodes])
  | exact reachFrame_getFrom away call framed root base hb _
  | exact reachFrame_unknown r R cells A _ _

private theorem reachFrame_hostMember {r R cells A} (name key : String) :
    ReachFrame r R cells A (SafeVal A) (hostMember name key : M W Val) := by
  unfold hostMember
  split
  · exact reachFrame_pure r R cells A _ (by simp [SafeVal, Val.nodes])
  · split
    · exact reachFrame_readHostRef r R cells A (.property name key)
    · exact reachFrame_unknown r R cells A _ _

private theorem stringOwn_safe {A s key v} (h : stringOwn s key = some (.ok v)) : SafeVal A v := by
  cases key with
  | wellKnownSymbol _ | symbol _ => cases h
  | str key =>
    simp only [stringOwn] at h
    split at h
    · cases h
      simp [SafeVal, Val.nodes]
    · simp only [bind, Option.bind, pure] at h
      split at h
      · cases h
      · dsimp only at h
        split at h
        · cases h
        · dsimp only at h
          split at h <;> cases h
          simp [SafeVal, Val.nodes]

theorem reachFrame_getProp {r R cells A} (away : Away r R A) (call : Call W)
    (framed : ∀ f base, SafeVal A f → SafeVal A base →
      ReachFrame r R cells A (SafeVal A) (call f base []))
    (root : A (.obj objectProto)) (base key : Val) (hb : SafeVal A base) :
    ReachFrame r R cells A (SafeVal A) (getProp call base key) := by
  cases base <;> simp only [getProp] <;>
    try first
    | exact reachFrame_throwErr r R cells A _ _ _
    | exact reachFrame_unknown r R cells A _ _
  case obj a =>
    exact (reachFrame_toKey r R cells A key).bind fun k _ =>
      reachFrame_getFrom away call framed (safeVal_obj.1 hb) _ hb k
  case str s =>
    apply (reachFrame_toKey r R cells A key).bind
    intro k _
    cases h : stringOwn s k with
    | none => exact reachFrame_protoProp away call framed root _ _ _ hb k
    | some value =>
      exact reachFrame_lift value (fun v hv => stringOwn_safe (hv ▸ h))
  case num =>
    exact (reachFrame_toKey r R cells A key).bind fun k _ =>
      reachFrame_protoProp away call framed root _ _ _ hb k
  case bool =>
    exact (reachFrame_toKey r R cells A key).bind fun k _ =>
      reachFrame_protoProp away call framed root _ _ _ hb k
  case host name =>
    apply (reachFrame_toKey r R cells A key).bind
    intro k _
    cases k with
    | str k => exact reachFrame_hostMember name k
    | wellKnownSymbol _ | symbol _ => exact reachFrame_unknown r R cells A _ _

/-- Reading a property preserves reachability when calls to retrieved getters do. -/
theorem perform_get {r R cells A} (away : Away r R A) (call : Call W)
    (construct : Val → List Val → M W Val) (code : Nat)
    (framed : ∀ f base, SafeVal A f → SafeVal A base →
      ReachFrame r R cells A (SafeVal A) (call f base []))
    (root : A (.obj objectProto)) (base key : WVal) :
    ReachFrame r R cells A (fun _ => True) (perform call construct code cells (.get base key)) :=
  (reachFrame_fromW r R cells A base).bind fun o ho =>
    (reachFrame_fromW r R cells A key).bind fun k _ =>
      (reachFrame_getProp away call framed root o k ho).bind fun result safe =>
        (reachFrame_toW r R cells A result safe).bind fun w _ => reachFrame_pure r R cells A (Told.value w) trivial

private theorem deleteProps_keeps_edges (o : Obj) (key : PropertyKey) :
    ∀ node ∈ ({ o with props := o.props.filter (·.1 != key) }).nodes, node ∈ o.nodes := by
  intro node hn
  simp only [Obj.nodes, List.mem_append] at hn ⊢
  rcases hn with ((h | h) | h) | h
  · exact Or.inl (Or.inl (Or.inl h))
  · obtain ⟨p, hp, hnode⟩ := List.mem_flatMap.mp h
    exact Or.inl (Or.inl (Or.inr (List.mem_flatMap.mpr ⟨p, (List.mem_filter.mp hp).1, hnode⟩)))
  · exact Or.inl (Or.inr h)
  · exact Or.inr h

theorem reachFrame_deleteProp {r R cells A} (away : Away r R A) (base key : Val) (hb : SafeVal A base) :
    ReachFrame r R cells A (SafeVal A) (deleteProp base key : M W Val) := by
  cases base <;> simp only [deleteProp] <;>
    try first
    | exact reachFrame_throwErr r R cells A _ _ _
    | exact reachFrame_unknown r R cells A _ _
  case obj a =>
    have ha := safeVal_obj.1 hb
    apply (reachFrame_toKey r R cells A key).bind
    intro k _
    apply (reachFrame_getObj away ha).bind
    intro o _
    have ok : ReachFrame r R cells A (SafeVal A) (pure (Val.bool true) : M W Val) :=
      reachFrame_pure r R cells A _ (by simp [SafeVal, Val.nodes])
    cases hk : o.props.lookup k with
    | none => exact ok
    | some p =>
      simp only []
      split
      · exact (reachFrame_modifyObj away a ha _ (fun o n h => Or.inl (deleteProps_keeps_edges o k n h))).bind
          (fun _ _ => ok)
      · exact reachFrame_throwErr r R cells A _ _ _
  case str =>
    apply (reachFrame_toKey r R cells A key).bind
    intro k _
    split
    · exact reachFrame_throwErr r R cells A _ _ _
    · exact reachFrame_pure r R cells A _ (by simp [SafeVal, Val.nodes])

/-- Deleting an own property removes edges from the reachable object. -/
theorem perform_delete {r R cells A} (away : Away r R A) (call : Call W)
    (construct : Val → List Val → M W Val) (code : Nat) (base key : WVal) :
    ReachFrame r R cells A (fun _ => True) (perform call construct code cells (.delete base key)) :=
  (reachFrame_fromW r R cells A base).bind fun o ho =>
    (reachFrame_fromW r R cells A key).bind fun k _ =>
      (reachFrame_deleteProp away o k ho).bind fun result safe =>
        (reachFrame_toW r R cells A result safe).bind fun w _ => reachFrame_pure r R cells A (Told.value w) trivial

def SafeDesc (A : HeapNode → Prop) (d : Desc) : Prop :=
  (∀ v, v ∈ d.value → SafeVal A v) ∧ (∀ v, v ∈ d.get → SafeVal A v) ∧
    (∀ v, v ∈ d.set → SafeVal A v)

private theorem safe_default {A : HeapNode → Prop} {v : Option Val} {fallback : Val}
    (hv : ∀ x, x ∈ v → SafeVal A x) (hf : SafeVal A fallback) : SafeVal A (v.getD fallback) := by
  cases v with
  | none => exact hf
  | some x => exact hv x rfl

private theorem applyDesc_nodes {A : HeapNode → Prop} (d : Desc) (hd : SafeDesc A d)
    {old : Option Property} (ho : ∀ p, p ∈ old → ∀ n ∈ p.nodes, A n)
    {p : Property} (h : applyDesc old d = .ok (some p)) : ∀ n ∈ p.nodes, A n := by
  have undef : SafeVal A .undef := fun _ hn => by cases hn
  have data (v : Val) (w e c : Bool) (hv : SafeVal A v) :
      ∀ n ∈ (Property.data (d.value.getD v) w e c).nodes, A n := safe_default hd.1 hv
  have accessor (g setter : Val) (e c : Bool) (hg : SafeVal A g) (hs : SafeVal A setter) :
      ∀ n ∈ (Property.accessor (d.get.getD g) (d.set.getD setter) e c).nodes, A n := by
    intro n hn
    rcases List.mem_append.mp hn with h | h
    · exact safe_default hd.2.1 hg _ h
    · exact safe_default hd.2.2 hs _ h
  cases old with
  | none =>
    simp only [applyDesc, pure, Except.pure] at h
    split at h
    · cases h; exact accessor .undef .undef _ _ undef undef
    · cases h; exact data .undef _ _ _ undef
  | some old =>
    have oldNodes := ho old rfl
    cases old with
    | data v w e c =>
      have vv : SafeVal A v := oldNodes
      simp [applyDesc, bind, Except.bind, pure, Except.pure] at h
      repeat' (split at h)
      all_goals cases h <;> first
        | exact data _ _ _ _ vv
        | exact accessor .undef .undef _ _ undef undef
    | accessor g setter e c =>
      have gg : SafeVal A g := fun n hn => oldNodes n (List.mem_append_left _ hn)
      have ss : SafeVal A setter := fun n hn => oldNodes n (List.mem_append_right _ hn)
      simp [applyDesc, bind, Except.bind, pure, Except.pure] at h
      repeat' (split at h)
      all_goals cases h <;> first
        | exact data .undef _ _ _ undef
        | exact accessor _ _ _ _ gg ss

private theorem setOwn_nodes (o : Obj) (k : PropertyKey) (p : Property) :
    ∀ node ∈ ({ o with props := setOwn o.props k p }).nodes, node ∈ o.nodes ∨ node ∈ p.nodes := by
  intro node h
  simp only [Obj.nodes, List.mem_append] at h ⊢
  rcases h with ((h | h) | h) | h
  · exact Or.inl (Or.inl (Or.inl (Or.inl h)))
  · obtain ⟨entry, member, edge⟩ := List.mem_flatMap.mp h
    unfold setOwn at member
    split at member
    · obtain ⟨old, hOld, rfl⟩ := List.mem_map.mp member
      rcases old with ⟨oldKey, oldProperty⟩
      by_cases same : (oldKey == k) = true
      · simp only [same, ↓reduceIte] at edge
        exact Or.inr edge
      · simp only [same] at edge
        exact Or.inl (Or.inl (Or.inl (Or.inr (List.mem_flatMap.mpr ⟨(oldKey, oldProperty), hOld, edge⟩))))
    · rcases List.mem_append.mp member with hOld | hNew
      · exact Or.inl (Or.inl (Or.inl (Or.inr (List.mem_flatMap.mpr ⟨entry, hOld, edge⟩))))
      · simp only [List.mem_singleton] at hNew
        subst entry
        exact Or.inr edge
  · exact Or.inl (Or.inl (Or.inr h))
  · exact Or.inl (Or.inr h)


private theorem defineOwnProps_nodes (growth : Option Nat) (o : Obj) (k : PropertyKey) (p : Property) :
    ∀ node ∈ ({ o with props := defineOwnProps growth o k p }).nodes, node ∈ o.nodes ∨ node ∈ p.nodes := by
  intro node edge
  cases growth with
  | none => exact setOwn_nodes o k p node edge
  | some len =>
    rcases setOwn_nodes { o with props := setOwn o.props k p } (.str "length")
      (.data (.num (Float.ofNat len)) true false false) node edge with old | impossible
    · exact setOwn_nodes o k p node old
    · simp [Property.nodes, Val.nodes] at impossible

private theorem reachFrame_applyDesc {r R cells A} (old : Option Property) (d : Desc)
    (safe : SafeDesc A d) (oldSafe : ∀ p, p ∈ old → ∀ node ∈ p.nodes, A node) :
    ReachFrame r R cells A (fun result => ∀ p, p ∈ result → ∀ node ∈ p.nodes, A node)
      (lift (applyDesc old d) : M W (Option Property)) := by
  apply reachFrame_lift
  intro result h p hp
  cases hp
  exact applyDesc_nodes d safe oldSafe h

theorem reachFrame_defineOwn {r R cells A} (away : Away r R A) {a : Addr} (ha : A (.obj a))
    (key : PropertyKey) (d : Desc) (safe : SafeDesc A d) :
    ReachFrame r R cells A (fun _ => True) (defineOwn a key d : M W Unit) := by
  unfold defineOwn
  apply (reachFrame_getObj away ha).bind
  intro current currentSafe
  split
  · exact reachFrame_throwErr r R cells A _ _ _
  split
  · exact reachFrame_unknown r R cells A _ _
  · have ownSafe : ∀ p, p ∈ current.props.lookup key → ∀ node ∈ p.nodes, A node := by
      intro p hp node edge
      have member := lookup_mem (show current.props.lookup key = some p from hp)
      exact currentSafe node (List.mem_append_left _ (List.mem_append_left _ (List.mem_append_right _
        (List.mem_flatMap.mpr ⟨(key, p), member, edge⟩))))
    apply (reachFrame_applyDesc (current.props.lookup key) d safe ownSafe).bind
    intro result guard
    cases result with
    | none => exact reachFrame_throwErr r R cells A _ _ _
    | some p =>
      exact reachFrame_modifyObj away a ha _ fun o node edge =>
        (defineOwnProps_nodes _ o key p node edge).imp id (guard p rfl node)

private theorem reachFrame_setValue {r R cells A} (away : Away r R A) {a : Addr} (ha : A (.obj a))
    (key : PropertyKey) (value : Val) (safe : SafeVal A value) :
    ReachFrame r R cells A (fun _ => True) (do
      defineOwn a key (if ((← getObj a).props.lookup key).isSome then { value := some value } else
        { value := some value, writable := some true, enumerable := some true, configurable := some true }) : M W Unit) := by
  apply (reachFrame_getObj away ha).bind
  intro o _
  have opt (v : Val) (hv : v ∈ some value) : SafeVal A v := by
    cases hv
    exact safe
  have empty (v : Val) (hv : v ∈ (none : Option Val)) : SafeVal A v := by cases hv
  split <;> exact reachFrame_defineOwn away ha key _ ⟨opt, empty, empty⟩

theorem reachFrame_setProp {r R cells A} (away : Away r R A) (call : Call W)
    (framed : ∀ f base v, SafeVal A f → SafeVal A base → SafeVal A v →
      ReachFrame r R cells A (SafeVal A) (call f base [v]))
    (base key value : Val) (safeBase : SafeVal A base) (safeValue : SafeVal A value) :
    ReachFrame r R cells A (fun _ => True) (setProp call base key value) := by
  cases base <;> simp only [setProp] <;>
    try first
    | exact reachFrame_throwErr r R cells A _ _ _
    | exact reachFrame_unknown r R cells A _ _
  case obj a =>
    have ha := safeVal_obj.1 safeBase
    apply (reachFrame_toKey r R cells A key).bind
    intro k _
    apply (reachFrame_lookupProp away ha k).bind
    intro result accessible
    cases result with
    | none => exact reachFrame_setValue away ha k value safeValue
    | some p =>
      have hp := accessible p rfl
      cases p with
      | data _ writable =>
        cases writable
        · exact reachFrame_throwErr r R cells A _ _ _
        · exact reachFrame_setValue away ha k value safeValue
      | accessor getter setter =>
        have hs : SafeVal A setter := fun n hn => hp n (List.mem_append_right _ hn)
        cases setter
        case undef => exact reachFrame_throwErr r R cells A _ _ _
        all_goals exact (framed _ _ _ hs safeBase safeValue).bind fun _ _ =>
          reachFrame_pure r R cells A () trivial
  case str =>
    apply (reachFrame_toKey r R cells A key).bind
    intro k _
    split
    · exact reachFrame_throwErr r R cells A _ _ _
    · exact reachFrame_unknown r R cells A _ _

/-- Setting a property keeps reachable edges local, provided invoking an accessible setter does. -/
theorem perform_set {r R cells A} (away : Away r R A) (call : Call W)
    (construct : Val → List Val → M W Val) (code : Nat)
    (framed : ∀ f base value, SafeVal A f → SafeVal A base → SafeVal A value →
      ReachFrame r R cells A (SafeVal A) (call f base [value]))
    (base key value : WVal) :
    ReachFrame r R cells A (fun _ => True) (perform call construct code cells (.set base key value)) :=
  (reachFrame_fromW r R cells A base).bind fun o ho =>
    (reachFrame_fromW r R cells A key).bind fun k _ =>
      (reachFrame_fromW r R cells A value).bind fun v hv =>
        (reachFrame_setProp away call framed o k v ho hv).bind fun _ _ =>
          reachFrame_pure r R cells A (Told.value (.prim .undef)) trivial


private theorem safe_arg {A : HeapNode → Prop} {args : List Val} (safe : ∀ x ∈ args, SafeVal A x) (i : Nat) :
    SafeVal A (args.getD i .undef) := by
  rw [List.getD_eq_getElem?_getD]
  cases h : args[i]? with
  | none => exact fun _ hn => by cases hn
  | some x => exact safe x (list_index_mem h)

theorem reachFrame_requireObj {r R cells A} (what : String) (v : Val) (safe : SafeVal A v) :
    ReachFrame r R cells A (fun a => A (.obj a)) (requireObj what v : M W Addr) := by
  cases v <;>
    first
    | exact reachFrame_unknown r R cells A _ _
    | exact reachFrame_throwErr r R cells A _ _ _
    | exact reachFrame_pure r R cells A _ (safeVal_obj.1 safe)

theorem reachFrame_isCallable {r R cells A} (away : Away r R A) (v : Val) (safe : SafeVal A v) :
    ReachFrame r R cells A (fun _ => True) (do pure (isCallable (← getState) v) : M W Bool) := by
  constructor
  · intro s H C _ _
    cases v <;> try rfl
    case obj a =>
      have ha : a.region ≠ r := away.region _ (safeVal_obj.1 safe)
      simp only [M.bind_apply, getState, isCallable, State.objAt_put, ite_eq_right ha]
      rfl
  · intro s _ _
    exact Preserves.refl r R s
  · intro s h _
    exact ⟨trivial, h⟩

private theorem reachFrame_descField {r R cells A} (away : Away r R A) (call : Call W)
    (framed : ∀ f base, SafeVal A f → SafeVal A base →
      ReachFrame r R cells A (SafeVal A) (call f base []))
    (root : A (.obj objectProto)) {a : Addr} (ha : A (.obj a)) (v : Val) (safe : SafeVal A v)
    (key : String) :
    ReachFrame r R cells A (fun o => ∀ x, x ∈ o → SafeVal A x) (do
      if (← lookupProp a key).isSome then some <$> getProp call v (.str key) else pure none) := by
  apply (reachFrame_lookupProp away ha key).bind
  intro result _
  split
  · exact (reachFrame_getProp away call framed root v (.str key) safe).bind fun x hx =>
      reachFrame_pure r R cells A (some x) (fun _ hx' => by cases hx'; exact hx)
  · exact reachFrame_pure r R cells A none (fun _ h => by cases h)

private theorem reachFrame_descFlag {r R cells A} {m : M W (Option Val)}
    (frame : ReachFrame r R cells A (fun o => ∀ x, x ∈ o → SafeVal A x) m) :
    ReachFrame r R cells A (fun _ => True) (do
      match ← m with
      | some x => some <$> lift (toBoolean x)
      | none => pure none) := by
  apply frame.bind
  intro result _
  cases result with
  | none => exact reachFrame_pure r R cells A none trivial
  | some x =>
    change ReachFrame r R cells A _ ((lift (toBoolean x)) >>= fun flag => pure (some flag))
    exact (reachFrame_lift (P := fun _ => True) (toBoolean x) (fun _ _ => trivial)).bind fun b _ =>
      reachFrame_pure r R cells A (some b) trivial

private theorem reachFrame_descCallable {r R cells A} (away : Away r R A) {m : M W (Option Val)}
    (frame : ReachFrame r R cells A (fun o => ∀ x, x ∈ o → SafeVal A x) m) (key : String) :
    ReachFrame r R cells A (fun o => ∀ x, x ∈ o → SafeVal A x) (do
      match ← m with
      | some (.host name) => unknown s!"host value {name} is inspected"
      | some .undef => pure (some .undef)
      | some f => if isCallable (← getState) f then pure (some f) else throwErr .type s!"{key}ter must be a function"
      | none => pure none) := by
  apply frame.bind
  intro result safe
  cases result with
  | none => exact reachFrame_pure r R cells A none safe
  | some f =>
    have hf := safe f rfl
    have check : ReachFrame r R cells A (fun o => ∀ x, x ∈ o → SafeVal A x) (do
        if (← (do pure (isCallable (← getState) f) : M W Bool)) then pure (some f)
        else throwErr .type s!"{key}ter must be a function") :=
      by
        apply (reachFrame_isCallable away f hf).bind
        intro yes _
        split
        · exact reachFrame_pure r R cells A (some f) safe
        · exact reachFrame_throwErr r R cells A _ .type s!"{key}ter must be a function"
    cases f
    case undef => exact reachFrame_pure r R cells A (some Val.undef) safe
    case host name => exact reachFrame_unknown r R cells A _ _
    all_goals exact check

private theorem reachFrame_toDesc {r R cells A} (away : Away r R A) (call : Call W)
    (framed : ∀ f base, SafeVal A f → SafeVal A base →
      ReachFrame r R cells A (SafeVal A) (call f base []))
    (root : A (.obj objectProto)) (v : Val) (safe : SafeVal A v) :
    ReachFrame r R cells A (SafeDesc A) (toDesc call v) := by
  unfold toDesc
  apply (reachFrame_requireObj "property description" v safe).bind
  intro a ha
  have field := reachFrame_descField away call framed root ha v safe
  apply (reachFrame_descFlag (field "enumerable")).bind
  intro enumerable _
  apply (reachFrame_descFlag (field "configurable")).bind
  intro configurable _
  apply (field "value").bind
  intro value hv
  apply (reachFrame_descFlag (field "writable")).bind
  intro writable _
  apply (reachFrame_descCallable away (field "get") "get").bind
  intro getter hg
  apply (reachFrame_descCallable away (field "set") "set").bind
  intro setter hs
  split
  · exact reachFrame_throwErr r R cells A _ _ _
  · exact reachFrame_pure r R cells A _ ⟨hv, hg, hs⟩

/-- Object.defineProperty only installs reachable descriptor values and accessor functions. -/
theorem reachFrame_prim_defineProperty {r R cells A} (away : Away r R A) (call : Call W)
    (framed : ∀ f base, SafeVal A f → SafeVal A base →
      ReachFrame r R cells A (SafeVal A) (call f base []))
    (root : A (.obj objectProto)) (args : List Val) (safe : ∀ x ∈ args, SafeVal A x) :
    ReachFrame r R cells A (SafeVal A) (prim call .defineProperty args) := by
  unfold prim
  apply (reachFrame_requireObj "Object.defineProperty target" _ (safe_arg safe 0)).bind
  intro a ha
  apply (reachFrame_toKey r R cells A _).bind
  intro key _
  apply (reachFrame_toDesc away call framed root _ (safe_arg safe 2)).bind
  intro desc hd
  exact (reachFrame_defineOwn away ha key desc hd).bind fun _ _ =>
    reachFrame_pure r R cells A _ (safe_arg safe 0)

private def prototypeTarget (target a : Addr) (_ : Obj) : Option Unit :=
  if a == target then some () else none

private theorem reaches_eq_findOnPrototype (s : State W) (target : Addr) {o : Obj}
    (present : s.objAt target = some o) (n : Nat) (start : Option Addr) :
    reaches s target n start =
      (start.bind (findOnPrototype s (prototypeTarget target) n)).isSome := by
  induction n generalizing start with
  | zero => cases start <;> rfl
  | succ n ih =>
    cases start with
    | none => rfl
    | some a =>
      by_cases same : a = target
      · subst a
        simp [reaches, findOnPrototype, prototypeTarget, present]
      · have different : (a == target) = false := by simpa using same
        cases h : s.objAt a with
        | none => simp [reaches, findOnPrototype, h, different]
        | some obj => simp [reaches, findOnPrototype, h, different, prototypeTarget, ih, same]

private theorem reaches_same {cells A} {s t : State W} (hs : Access cells A s) (ht : Access cells A t)
    (same : ∀ a, A (.obj a) → s.objAt a = t.objAt a)
    {target : Addr} {o : Obj} (present : s.objAt target = some o) (ha : A (.obj target))
    (parent : Option Addr) (parentSafe : ∀ a ∈ parent, A (.obj a)) :
    reaches s target s.objectCount parent = reaches t target t.objectCount parent := by
  rw [reaches_eq_findOnPrototype s target present,
    reaches_eq_findOnPrototype t target ((same _ ha).symm.trans present)]
  cases parent with
  | none => rfl
  | some p =>
    simp only [Option.bind_some]
    rw [findOnPrototype_same hs ht same _ (parentSafe p rfl)]

theorem reachFrame_chainContains {r R cells A} (away : Away r R A) {target : Addr}
    (ht : A (.obj target)) (parent : Option Addr) (hp : ∀ a ∈ parent, A (.obj a)) :
    ReachFrame r R cells A (fun _ => True) (chainContains target parent : M W Bool) := by
  constructor
  · intro s H C hs _
    let t := s.put r H C
    have same (a : Addr) (ha : A (.obj a)) : s.objAt a = t.objAt a := by
      have excluded : a.region ≠ r := away.region _ ha
      rw [State.objAt_put, ite_eq_right excluded]
    simp only [chainContains, M.bind_apply, getObj, getState]
    rw [← same target ht]
    cases present : s.objAt target with
    | none => rfl
    | some o =>
      dsimp only
      rw [reaches_same hs (hs.put away H C) same present ht parent hp]
      rfl
  · intro s _ _
    simp only [chainContains, M.bind_apply, getObj, getState]
    cases s.objAt target <;> exact Preserves.refl r R s
  · intro s hs _
    simp only [chainContains, M.bind_apply, getObj, getState]
    cases s.objAt target <;> first | exact hs | exact ⟨trivial, hs⟩

private theorem reachFrame_checkPrototype {r R cells A} (away : Away r R A)
    {target : Addr} (safeTarget : A (.obj target)) (parent : Option Addr)
    (safeParent : ∀ a ∈ parent, A (.obj a)) :
    ReachFrame r R cells A (fun _ => True) (do
      let s ← getState
      let object ← getObj target
      if object.proto == parent then pure false
      else if object.frozen || reaches s target s.objectCount parent then
        throwErr .type (if object.frozen then "cannot change the prototype of a frozen object" else "cyclic __proto__ value")
      else pure true : M W Bool) := by
  constructor
  · intro s H C hs _
    let t := s.put r H C
    have same (a : Addr) (ha : A (.obj a)) : s.objAt a = t.objAt a := by
      have excluded : a.region ≠ r := away.region _ ha
      rw [State.objAt_put, ite_eq_right excluded]
    have sameTarget : (s.put r H C).objAt target = s.objAt target := (same target safeTarget).symm
    simp only [M.bind_apply, getState, getObj]
    rw [sameTarget]
    cases hs' : s.objAt target with
    | none => rfl
    | some o =>
      dsimp only
      have cycle := reaches_same hs (hs.put away H C) same hs' safeTarget parent safeParent
      rw [← cycle]
      split
      · rfl
      · split <;> rfl
  · intro s _ _
    simp only [M.bind_apply, getState, getObj]
    cases s.objAt target with
    | none => exact Preserves.refl r R s
    | some o =>
      dsimp only
      split
      · exact Preserves.refl r R s
      · split <;> exact Preserves.refl r R s
  · intro s hs _
    simp only [M.bind_apply, getState, getObj]
    cases s.objAt target with
    | none => exact hs
    | some o =>
      dsimp only
      split
      · exact ⟨trivial, hs⟩
      · split
        · exact ⟨(fun _ hn => by cases hn), hs⟩
        · exact ⟨trivial, hs⟩

private theorem reachFrame_setPrototype {r R cells A} (away : Away r R A)
    {target : Addr} (safeTarget : A (.obj target)) (parent : Option Addr)
    (safeParent : ∀ a ∈ parent, A (.obj a)) :
    ReachFrame r R cells A (SafeVal A) (do
      let s ← getState
      let object ← getObj target
      if object.proto == parent then pure (.obj target)
      else if object.frozen || reaches s target s.objectCount parent then
        throwErr .type (if object.frozen then "cannot change the prototype of a frozen object" else "cyclic __proto__ value")
      else
        modifyObj target fun o => { o with proto := parent }
        pure (.obj target) : M W Val) := by
  have operation : ReachFrame r R cells A (SafeVal A) (do
      let update ← (do
        let s ← getState
        let object ← getObj target
        if object.proto == parent then pure false
        else if object.frozen || reaches s target s.objectCount parent then
          throwErr .type (if object.frozen then "cannot change the prototype of a frozen object" else "cyclic __proto__ value")
        else pure true)
      if update then modifyObj target fun o => { o with proto := parent }
      pure (.obj target) : M W Val) := by
    apply (reachFrame_checkPrototype away safeTarget parent safeParent).bind
    intro update _
    cases update
    · exact reachFrame_pure r R cells A _ (safeVal_obj.2 safeTarget)
    · apply (reachFrame_modifyObj away target safeTarget _ ?_).bind
      · intro _ _
        exact reachFrame_pure r R cells A _ (safeVal_obj.2 safeTarget)
      · intro o node h
        simp only [Obj.nodes, List.mem_append] at h
        rcases h with ((hp | props) | code) | promise
        · obtain ⟨a, ha, rfl⟩ := List.mem_map.mp hp
          exact Or.inr (safeParent a (Option.mem_toList.mp ha))
        · exact Or.inl (List.mem_append_left _ (List.mem_append_left _ (List.mem_append_right _ props)))
        · exact Or.inl (List.mem_append_left _ (List.mem_append_right _ code))
        · exact Or.inl (List.mem_append_right _ promise)
  refine Eq.mpr (congrArg (ReachFrame r R cells A (SafeVal A)) ?_) operation
  funext s
  simp only [M.bind_apply, getState, getObj]
  cases s.objAt target with
  | none => rfl
  | some o =>
    dsimp only
    split
    · rfl
    · split <;> rfl

/-- The built-in prototype read exposes only a reachable prototype. -/
theorem reachFrame_prim_getPrototypeOf {r R cells A} (away : Away r R A) (call : Call W)
    (args : List Val) (safe : ∀ x ∈ args, SafeVal A x) :
    ReachFrame r R cells A (SafeVal A) (prim call .getPrototypeOf args) := by
  have arg := safe_arg safe 0
  simp only [prim]
  generalize (args.getD 0 .undef) = v at arg ⊢
  cases v <;> simp only [] <;>
    try first
    | exact reachFrame_throwErr r R cells A _ _ _
    | exact reachFrame_unknown r R cells A _ _
  case obj a =>
    apply (reachFrame_getObj away (safeVal_obj.1 arg)).bind
    intro o ho
    cases h : o.proto with
    | none =>
      exact reachFrame_pure r R cells A Val.null (fun _ hn => by cases hn)
    | some p =>
      exact reachFrame_pure r R cells A (Val.obj p) (safeVal_obj.2 (ho _ (by simp [Obj.nodes, h])))

private theorem reachFrame_prototypeArg {r R cells A} (v : Val) (safe : SafeVal A v) :
    ReachFrame r R cells A (fun parent => ∀ a ∈ parent, A (.obj a)) (match v with
      | .null => pure none
      | v => some <$> requireObj "object prototype" v : M W (Option Addr)) := by
  have require := (reachFrame_requireObj (W := W) (r := r) (R := R) (cells := cells) "object prototype" v safe).bind
    (fun a ha => reachFrame_pure r R cells A (P := fun parent => ∀ p ∈ parent, A (.obj p)) (some a)
      (fun p hp => by cases hp; exact ha))
  cases v
  case null => exact reachFrame_pure r R cells A none (fun _ h => by cases h)
  all_goals exact require

/-- A prototype update checks the actual accessible chain for a cycle before installing its edge. -/
theorem reachFrame_prim_setPrototypeOf {r R cells A} (away : Away r R A) (call : Call W)
    (args : List Val) (safe : ∀ x ∈ args, SafeVal A x) :
    ReachFrame r R cells A (SafeVal A) (prim call .setPrototypeOf args) := by
  have base := safe_arg safe 0
  simp only [prim]
  generalize args.getD 0 .undef = v at base ⊢
  let finish (parent : Option Addr) : M W Val := match v with
    | .obj target => do
      let s ← getState
      let object ← getObj target
      if object.proto == parent then pure v
      else if object.frozen || reaches s target s.objectCount parent then
        throwErr .type (if object.frozen then "cannot change the prototype of a frozen object" else "cyclic __proto__ value")
      else
        modifyObj target fun o => { o with proto := parent }
        pure v
    | .host name => unknown s!"host value {name} is inspected"
    | .error .. => unknown "an engine error is inspected"
    | v => pure v
  have framed (parent : Option Addr) (hp : ∀ a ∈ parent, A (.obj a)) :
      ReachFrame r R cells A (SafeVal A) (finish parent) := by
    cases v
    case obj a => exact reachFrame_setPrototype away (safeVal_obj.1 base) parent hp
    case host name => exact reachFrame_unknown r R cells A _ _
    case error kind msg => exact reachFrame_unknown r R cells A _ _
    all_goals exact reachFrame_pure r R cells A _ base
  have hp := safe_arg safe 1
  generalize (args.getD 1 .undef) = p at hp ⊢
  have update := (reachFrame_prototypeArg p hp).bind framed
  cases v
  case undef => exact reachFrame_throwErr r R cells A _ _ _
  case null => exact reachFrame_throwErr r R cells A _ _ _
  all_goals cases p <;> exact update

end CoreJS
