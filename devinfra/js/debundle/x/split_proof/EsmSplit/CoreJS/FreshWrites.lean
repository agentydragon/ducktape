import EsmSplit.CoreJS.ClassOrigin

/-! # Strict writes to fresh own properties

The reduction compares complete CoreJS states. An inherited setter or a repeated
key requires a different argument. -/

namespace CoreJS

/-- Strict assignment adds an own writable, enumerable, configurable data property
when lookup misses throughout the prototype chain of an ordinary object. -/
theorem setProp_fresh_data {s : State W} {a : Addr} {o : Obj} {key : String}
    (obj : s.objAt a = some o) (ordinary : o.array = false) (extensible : o.frozen = false)
    (absent : findProp s s.objectCount a (.str key) = none)
    (own : o.props.lookup (PropertyKey.str key) = none) (call : Call W) (value : Val) :
    setProp call (.obj a) (.str key) value s =
      modifyObj a
        (fun obj => { obj with props := setOwn obj.props (.str key) (.data value true true true) })
        s := by
  have converted : (toKey (.str key) : M W PropertyKey) = pure (.str key) := rfl
  simp [setProp, converted, lift, lookupProp, getState, bind, pure, Except.pure, absent, getObj,
    obj, defineOwn, ownIndexWrite, defineOwnProps, ordinary, extensible, own, applyDesc]

private theorem key_absent (props : List (PropertyKey × Property)) (key : PropertyKey)
    (h : key ∉ props.map Prod.fst) : props.lookup key = none ∧ props.any (·.1 == key) = false := by
  refine ⟨lookup_none_iff_not_mem_keys.mpr h, List.any_eq_false.mpr ?_⟩
  intro field member
  exact fun eq => h (beq_iff_eq.mp eq ▸ List.mem_map_of_mem member)

private theorem findProp_absent {s : State W} (obj : s.objAt a = some o)
    (proto : o.proto = some parent) (root : s.objAt parent = some p) (top : p.proto = none)
    (h : o.props.lookup key = none) (hp : p.props.lookup key = none) (n : Nat) :
    findProp s n a key = none := by
  cases n with
  | zero => rfl
  | succ n => cases n <;> simp [findProp, obj, proto, root, top, h, hp]

private theorem modify_id (xs : Array α) (i : Nat) : xs.modify i id = xs := by
  apply Array.ext_getElem?
  intro j
  by_cases h : i = j <;> simp [Array.getElem?_modify, h]

private theorem modify_twice (xs : Array α) (i : Nat) (f g : α → α) :
    (xs.modify i f).modify i g = xs.modify i (g ∘ f) := by
  apply Array.ext_getElem?
  intro j
  by_cases h : i = j <;> simp [Array.getElem?_modify, h, Option.map_map]

private theorem modify_at_known (xs : Array α) (i : Nat) (f g : α → α)
    (known : ∀ x, xs[i]? = some x → f x = g x) : xs.modify i f = xs.modify i g := by
  apply Array.ext_getElem?
  intro j
  by_cases h : i = j
  · subst j
    cases get : xs[i]? with
    | none => simp [Array.getElem?_modify, get]
    | some x => simp [Array.getElem?_modify, get, known x get]
  · simp [Array.getElem?_modify, h]

/-- A sequence of strict writes of distinct, fresh string keys has the exact effect of
one heap update appending writable, enumerable, configurable data properties in order.
The ordinary receiver's prototype has no parent and contains none of the keys. -/
theorem setProp_many_fresh {s : State W} (call : Call W) (fields : List (String × Val))
    (obj : s.objAt a = some o) (ordinary : o.array = false) (extensible : o.frozen = false)
    (proto : o.proto = some parent) (hne : parent ≠ a) (root : s.objAt parent = some p)
    (top : p.proto = none) (unique : (fields.map Prod.fst).Nodup)
    (fresh : ∀ f ∈ fields, PropertyKey.str f.1 ∉ o.props.map Prod.fst)
    (inherited : ∀ f ∈ fields, p.props.lookup (PropertyKey.str f.1) = none) :
    (fields.forM fun (k, v) => setProp call (.obj a) (.str k) v) s =
      modifyObj a
        (fun old =>
          { old with
            props :=
              old.props ++
                fields.map (fun (k, v) => (PropertyKey.str k, Property.data v true true true)) })
        s := by
  induction fields generalizing s o with
  | nil =>
    simp only [modifyObj, List.map_nil, List.append_nil]
    change
      Res.ok () s =
        Res.ok () { s with heap := s.heap.modify a.region (fun r => r.modify a.index id) }
    simp only [modify_id]
    rw [show s.heap.modify a.region (fun r => r) = s.heap from modify_id _ _]
  | cons head rest ih =>
    rcases head with ⟨k, v⟩
    simp only [List.map_cons, List.nodup_cons] at unique
    obtain ⟨absent, noKey⟩ := key_absent o.props (.str k) (fresh (k, v) (by simp))
    have miss :=
      findProp_absent obj proto root top absent (inherited (k, v) (by simp)) s.objectCount
    let add := fun (old : Obj) =>
      { old with props := setOwn old.props (.str k) (.data v true true true) }
    let u := { s with heap := s.heap.modify a.region (·.modify a.index add) }
    have first : setProp call (.obj a) (.str k) v s = .ok () u :=
      setProp_fresh_data obj ordinary extensible miss absent call v
    have changed : u.objAt a = some (add o) :=
      modifyObj_self obj (show modifyObj a add s = .ok () u from rfl)
    have other (b : Addr) (ne : b ≠ a) : u.objAt b = s.objAt b := objAt_modify _ _ _ _ ne
    have props :
      (add o).props = o.props ++ [(PropertyKey.str k, Property.data v true true true)] := by
      simp [add, setOwn, noKey]
    have stillFresh : ∀ f ∈ rest, PropertyKey.str f.1 ∉ (add o).props.map Prod.fst := by
      intro f hf
      have diff : f.1 ≠ k := fun eq => unique.1 (eq ▸ List.mem_map.mpr ⟨f, hf, rfl⟩)
      simpa [props, diff] using fresh f (by simp [hf])
    have restRun :=
      ih changed ordinary extensible proto (other parent hne ▸ root) unique.2 stillFresh
        (fun f hf => inherited f (by simp [hf]))
    change
      (setProp call (.obj a) (.str k) v >>= fun _ =>
            rest.forM (fun (k, v) => setProp call (.obj a) (.str k) v))
          s =
        _
    rw [bind_of_ok first, restRun]
    simp only [modifyObj, u, modify_twice, List.map_cons, Res.ok.injEq, true_and]
    congr 1
    apply modify_at_known
    intro region hr
    change (region.modify a.index add).modify a.index _ = _
    rw [modify_twice]
    apply modify_at_known
    intro old ho
    have : old = o := by simpa [State.objAt, hr, ho] using obj
    subst old
    simp [add, setOwn, noKey, List.append_assoc]

end CoreJS
