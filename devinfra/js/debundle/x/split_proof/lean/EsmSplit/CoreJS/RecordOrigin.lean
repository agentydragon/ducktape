import EsmSplit.CoreJS.ClassOrigin

/-! # Own data descriptors established by a fresh record literal

Keys are literal strings and distinct. Values may allocate or read bindings. -/

namespace CoreJS
namespace RecordOrigin

def property (kv : String × Expr) : PropDef := .init (.str kv.1) kv.2

private def dataProps (xs : List (String × Val)) : List (String × Property) :=
  xs.map fun (k, v) => (k, .data v true true true)

private theorem fresh_key {xs : List (String × Val)} (h : k ∉ xs.map Prod.fst) :
    (dataProps xs).lookup k = none ∧ (dataProps xs).any (·.1 == k) = false := by
  induction xs with
  | nil => exact ⟨rfl, rfl⟩
  | cons a rest ih =>
    rcases a with ⟨key, value⟩
    simp only [List.map_cons, List.mem_cons, not_or] at h
    obtain ⟨hl, ha⟩ := ih h.2
    change (((key, Property.data value true true true) :: dataProps rest).lookup k = none) ∧
      ((key == k) || (dataProps rest).any (·.1 == k)) = false
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
  simp only [record, hl] at h
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
    ∃ v, (record ys).props.lookup k = some (.data v true true true) := by
  induction ys with
  | nil => cases hk
  | cons f tail ih =>
    rcases f with ⟨key, value⟩
    by_cases heq : k = key
    · subst key; exact ⟨value, by simp [record, dataProps]⟩
    · simp only [List.map_cons, List.mem_cons] at hk
      obtain ⟨v, hv⟩ := ih (hk.resolve_left heq)
      exact ⟨v, by simpa only [record, dataProps, List.map_cons, List.lookup_cons,
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
      ∀ k ∈ fs.map Prod.fst, ∃ v, o.props.lookup k = some (.data v true true true) := by
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

end RecordOrigin
end CoreJS
