import Experiments.CoreJS.Scope

namespace CoreJS

variable {W : World}

theorem State.objectCount_pos_of_objAt {s : State W} {a : Addr} {o : Obj}
    (hObj : s.objAt a = some o) : 0 < s.objectCount := by
  unfold State.objAt at hObj
  obtain ⟨region, hRegion, hLookup⟩ := Option.bind_eq_some_iff.mp hObj
  have hIndex := (Array.getElem?_eq_some_iff.mp hLookup).1
  rw [State.objectCount, ← Array.foldl_toList,
    ← List.foldl_map (f := Array.size) (g := (· + ·)), ← List.sum_eq_foldl]
  exact List.sum_pos_iff_exists_pos_nat.mpr
    ⟨region.size, List.mem_map_of_mem (f := Array.size)
      (Array.mem_toList_iff.mpr (Array.mem_of_getElem? hRegion)), Nat.zero_lt_of_lt hIndex⟩

/-- An own data `prototype` is found at the first lookup step. Heritage evaluation returns its address without
invoking a getter or changing the state. -/
theorem classParents_ownPrototype {s : State W} {a p : Addr} {o : Obj} {f : Func} {closed : Env}
    {fields : List (String × Expr)} {w e c : Bool} (call : Call W) (hObj : s.objAt a = some o)
    (hCode : o.code = some (.closure f closed fields)) (hKind : f.kind = .classCtor)
    (hProto : o.props.lookup "prototype" = some (.data (.obj p) w e c)) :
    classParents call (.obj a) s = .ok (a, some p) s := by
  have hConstructor : isConstructor (.obj a) s = .ok true s := by
    simp only [isConstructor, M.bind_apply, getObj, hObj, hCode, hKind] <;> rfl
  have hLookup : lookupProp a "prototype" s = .ok (some (.data (.obj p) w e c)) s := by
    change Res.ok (findProp s s.objectCount a "prototype") s = _
    have hCount := State.objectCount_pos_of_objAt hObj
    cases hFuel : s.objectCount with
    | zero => simp [hFuel] at hCount
    | succ n => simp only [findProp, hObj, hProto]
  have hGet : getProp call (.obj a) (.str "prototype") s = .ok (.obj p) s := by
    change getFrom call a (.obj a) "prototype" s = _
    simp only [getFrom, M.bind_apply, hLookup] <;> rfl
  simp [classParents, M.bind_apply, hConstructor, hGet]
  rfl

theorem classParents_extends {s t : State W} {a p : Addr} {o : Obj} {f : Func} {closed : Env}
    {fields : List (String × Expr)} {w e c : Bool} (call : Call W) (h : Extends s t)
    (hObj : s.objAt a = some o) (hCode : o.code = some (.closure f closed fields)) (hKind : f.kind = .classCtor)
    (hProto : o.props.lookup "prototype" = some (.data (.obj p) w e c)) :
    classParents call (.obj a) t = .ok (a, some p) t :=
  classParents_ownPrototype call (h.objs a o hObj) hCode hKind hProto

/-- Either state can replay heritage lookup when both already contain the constructor record. -/
theorem classParents_replay {W' : World} {s : State W} {t : State W'} {a p : Addr} {o : Obj} {f : Func}
    {closed : Env} {fields : List (String × Expr)} {w e c : Bool} {parents : Addr × Option Addr}
    (call : Call W) (call' : Call W') (hObj : s.objAt a = some o) (hObj' : t.objAt a = some o)
    (hCode : o.code = some (.closure f closed fields)) (hKind : f.kind = .classCtor)
    (hProto : o.props.lookup "prototype" = some (.data (.obj p) w e c)) :
    classParents call (.obj a) s = .ok parents s ↔ classParents call' (.obj a) t = .ok parents t := by
  rw [classParents_ownPrototype call hObj hCode hKind hProto,
    classParents_ownPrototype call' hObj' hCode hKind hProto]
  simp

end CoreJS
