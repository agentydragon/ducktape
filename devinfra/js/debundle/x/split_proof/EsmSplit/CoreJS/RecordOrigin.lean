import EsmSplit.CoreJS.ClassOrigin

/-! # Own data descriptors established by a fresh record literal

Keys are literal strings and distinct. Values may allocate or read bindings. -/

namespace CoreJS
namespace RecordOrigin

def property (kv : String × Expr) : PropDef := .init (.str kv.1) kv.2

private def dataProps (xs : List (String × Val)) : List (PropertyKey × Property) :=
  xs.map fun (k, v) => (k, .data v true true true)

private theorem fresh_key {xs : List (String × Val)} (h : k ∉ xs.map Prod.fst) :
    (dataProps xs).lookup (PropertyKey.str k) = none ∧ (dataProps xs).any (·.1 == k) = false := by
  induction xs with
  | nil => exact ⟨rfl, rfl⟩
  | cons a rest ih =>
    rcases a with ⟨key, value⟩
    simp only [List.map_cons, List.mem_cons, not_or] at h
    obtain ⟨hl, ha⟩ := ih h.2
    change (((PropertyKey.str key, Property.data value true true true) :: dataProps rest).lookup (PropertyKey.str k) = none) ∧
      ((PropertyKey.str key == .str k) || (dataProps rest).any (·.1 == k)) = false
    have hne : key ≠ k := Ne.symm h.1
    simp [List.lookup_cons, beq_eq_false_iff_ne.mpr h.1, hne, hl, ha]

private def record (xs : List (String × Val)) : Obj :=
  { proto := some objectProto, props := dataProps xs }

private theorem add_fresh {s t : State W} (ho : s.objAt a = some (record xs))
    (fresh : k ∉ xs.map Prod.fst) (h : createData a k v s = .ok () t) :
    t.objAt a = some (record (xs ++ [(k, v)])) := by
  obtain ⟨hl, ha⟩ := fresh_key fresh
  have hg : getObj a s = .ok (record xs) s := by simp [getObj, ho]
  unfold createData defineOwn at h
  rw [bind_of_ok hg] at h
  simp [record, hl, ownIndexWrite, defineOwnProps, applyDesc] at h
  change modifyObj a (fun o => { o with props := setOwn o.props k (.data v true true true) }) s = .ok () t at h
  have hm := modifyObj_self ho h
  simp only [record, setOwn, ha, Bool.false_eq_true, ite_false] at hm
  simpa [record, dataProps] using hm

private theorem fields_origin (fuel : Nat) (env : Env) (fs : List (String × Expr)) (xs : List (String × Val))
    {s t : State W} (ho : s.objAt a = some (record xs))
    (fresh : (fs.map Prod.fst).Nodup)
    (sep : ∀ f ∈ fs, f.1 ∉ xs.map Prod.fst)
    (localValues : ∀ f ∈ fs, f.2.allocOnly = true)
    (run : evalProps fuel env a (fs.map property) s = .ok () t) :
    ∃ ys, ys.map Prod.fst = fs.map Prod.fst ∧ t.objAt a = some (record (xs ++ ys)) := by
  induction fuel generalizing fs xs s t with
  | zero => cases run
  | succ n ih =>
    cases fs with
    | nil => cases run; exact ⟨[], rfl, by simpa using ho⟩
    | cons f rest =>
      rcases f with ⟨k, e⟩
      simp only [List.map_cons, List.nodup_cons] at fresh
      simp only [List.map_cons, property, evalProps] at run
      cases n with
      | zero => cases run
      | succ m =>
        simp only [evalExpr, M.pure_bind] at run
        obtain ⟨_, _, hkey, run⟩ := bind_ok run
        cases hkey
        obtain ⟨v, s1, he, run⟩ := bind_ok run
        obtain ⟨_, _, hdata, hrest⟩ := bind_ok run
        have keep := allocOnly_extends (W := W) (fuel := m + 1) (env := env) (s := s) (localValues _ List.mem_cons_self)
        change evalExpr (m + 1) env e s = .ok v s1 at he
        change (evalExpr (m + 1) env e s).Keeps s at keep
        rw [he] at keep
        have hoo := add_fresh (keep.objs _ _ ho) (sep _ List.mem_cons_self) hdata
        obtain ⟨ys, hkeys, hobj⟩ := ih rest (xs ++ [(k, v)]) hoo fresh.2 (by
          intro g hg
          simp only [List.map_append, List.map_cons, List.map_nil, List.mem_append,
            List.mem_singleton, not_or]
          exact ⟨sep _ (List.mem_cons_of_mem _ hg), fun h => fresh.1 (List.mem_map.mpr ⟨g, hg, h⟩)⟩)
          (fun g hg => localValues _ (List.mem_cons_of_mem _ hg)) hrest
        exact ⟨(k, v) :: ys, by simp [hkeys], by simpa [List.append_assoc] using hobj⟩

private theorem lookup_record (ys : List (String × Val)) (k : String) (hk : k ∈ ys.map Prod.fst) :
    ∃ v, (record ys).props.lookup (PropertyKey.str k) = some (.data v true true true) := by
  induction ys with
  | nil => cases hk
  | cons f tail ih =>
    rcases f with ⟨key, value⟩
    by_cases heq : k = key
    · subst key; exact ⟨value, by simp [record, dataProps]⟩
    · simp only [List.map_cons, List.mem_cons] at hk
      obtain ⟨v, hv⟩ := ih (hk.resolve_left heq)
      exact ⟨v, by simpa only [record, dataProps, List.map_cons, List.lookup_cons, PropertyKey.str_beq,
        beq_eq_false_iff_ne.mpr heq] using hv⟩

/-- A successful var declaration establishes a binding to an object in the
declaration's region. Every source key has an own writable, enumerable,
configurable data property, including when its initializer allocated. -/
theorem literalVar_origin {n id : Nat} {env : Env} {x : Name} {fs : List (String × Expr)}
    (fresh : (fs.map Prod.fst).Nodup) (localValues : ∀ f ∈ fs, f.2.allocOnly = true)
    {s t : State W} {flow : Flow}
    (run : runUnit n env id (.decl .var x (.obj (fs.map property))) s = .ok flow t) :
    ∃ cell a o, env.vars.lookup x = some cell ∧ t.cellAt cell = some ⟨some (.obj a), true⟩ ∧
      a.region = id + 1 ∧ t.objAt a = some o ∧
      ∀ k ∈ fs.map Prod.fst, ∃ v, o.props.lookup (PropertyKey.str k) = some (.data v true true true) := by
  obtain ⟨u, hexec, rfl⟩ := runUnit_ok run
  cases n with
  | zero => cases hexec
  | succ n =>
    unfold execStmt at hexec
    obtain ⟨v, s1, hobj, hw⟩ := bind_ok hexec
    obtain ⟨_, t1, hwrite, hf⟩ := bind_ok hw
    cases hf
    cases n with
    | zero => cases hobj
    | succ n =>
      unfold evalExpr at hobj
      obtain ⟨a, t2, ha, h⟩ := bind_ok hobj
      obtain ⟨_, t3, hp, hv⟩ := bind_ok h
      cases hv
      obtain ⟨ys, hkeys, hob⟩ := fields_origin n env fs [] (alloc_objAt ha) fresh (by simp) localValues hp
      obtain ⟨cell, hl, hb, hh⟩ := writeVar_origin hwrite
      refine ⟨cell, a, record ys, hl, hb, alloc_region ha, ?_, ?_⟩
      · simpa [State.objAt, hh] using hob
      · intro k hk
        exact lookup_record ys k (hkeys.symm ▸ hk)

/-- Literal keys must be distinct own data entries. This specialization
retains the actual Number, rather than just the existence of a descriptor. -/
def numericField (field : String × Float) : PropDef := .init (.str field.1) (.num field.2)
def numberValues (fields : List (String × Float)) := fields.map fun (k, x) => (k, Val.num x)

private theorem numericFields_origin (n : Nat) (env : Env) (fields : List (String × Float)) (previous : List (String × Val))
    {s t : State W} (ho : s.objAt a = some (record previous))
    (fresh : (fields.map Prod.fst).Nodup) (sep : ∀ f ∈ fields, f.1 ∉ previous.map Prod.fst)
    (run : evalProps n env a (fields.map numericField) s = .ok () t) :
    t.objAt a = some (record (previous ++ numberValues fields)) := by
  induction n generalizing fields previous s t with
  | zero => cases run
  | succ n ih =>
    cases fields with
    | nil => cases run; simpa [numberValues] using ho
    | cons f fs =>
      obtain ⟨k, x⟩ := f
      cases n with
      | zero => cases run
      | succ n =>
        change (createData a (.str k) (.num x) >>= fun _ =>
          evalProps (n + 1) env a (fs.map numericField)) s = .ok () t at run
        obtain ⟨_, u, write, rest⟩ := bind_ok run
        simp only [List.map_cons, List.nodup_cons] at fresh
        have next := add_fresh ho (sep _ List.mem_cons_self) write
        have result := ih fs (previous ++ [(k, .num x)]) next fresh.2 (by
          intro f hf
          simp only [List.map_append, List.map_cons, List.map_nil, List.mem_append, List.mem_singleton, not_or]
          exact ⟨sep f (List.mem_cons_of_mem _ hf), fun eq => fresh.1 (List.mem_map.mpr ⟨f, hf, eq⟩)⟩) rest
        simpa [numberValues, List.append_assoc] using result

private theorem lookupNumber (fs : List (String × Float)) (k : String) :
    (record (numberValues fs)).props.lookup (PropertyKey.str k) =
      (fs.lookup k).map (fun x => Property.data (.num x) true true true) := by
  induction fs with
  | nil => rfl
  | cons f fs ih =>
    obtain ⟨key, x⟩ := f
    by_cases h : k = key
    · subst key; simp [record, dataProps, numberValues]
    · simpa only [record, dataProps, numberValues, List.map_cons, List.lookup_cons, PropertyKey.str_beq,
        beq_eq_false_iff_ne.mpr h] using ih

/-- Successful evaluation of the ordinary object expression establishes the
witness about the actual allocated address. No assumption about the incoming
heap or host is required. -/
theorem numericRecord_origin (fs : List (String × Float)) (fresh : (fs.map Prod.fst).Nodup)
    {s t : State W} (run : evalExpr n env (.obj (fs.map numericField)) s = .ok v t) :
    ∃ a o, v = .obj a ∧ t.objAt a = some o ∧
      ∀ k x, fs.lookup k = some x → o.props.lookup (.str k) = some (.data (.num x) true true true) := by
  cases n with
  | zero => cases run
  | succ n =>
    unfold evalExpr at run
    obtain ⟨a, s1, allocates, run⟩ := bind_ok run
    obtain ⟨_, s2, fields, result⟩ := bind_ok run
    cases result
    have complete := numericFields_origin n env fs [] (alloc_objAt allocates) fresh (by simp) fields
    refine ⟨a, record (numberValues fs), rfl, complete, ?_⟩
    intro k x found
    rw [lookupNumber, found]; rfl

end RecordOrigin
end CoreJS
