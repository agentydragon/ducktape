import Lean.Meta.Tactic.Cbv.Opaque
import EsmSplit.CoreJS.ClassOrigin

/-! # Bulk reduction of a fresh record with static string fields

The ordinary evaluator handles every object expression. A record of literal strings
can be reduced in one allocation when the original fuel covers all elements.
The certificate uses the reduction when comparing linked executions. -/

namespace CoreJS

variable {W : World}

private abbrev Fields := List (String × String)

/-- The original object-expression syntax for a string field. -/
def literalField (pair : String × String) : PropDef :=
  .init (.str pair.1) (.str pair.2)

theorem freeProps_literalFields (fields : List (String × String)) : freeProps (fields.map literalField) = [] := by
  induction fields with
  | nil => rfl
  | cons head tail ih =>
    simpa only [List.map_cons, freeProps, literalField, PropDef.free, Expr.free, List.nil_append] using ih

/-- The descriptor created by an ordinary string data property. -/
def literalDataProperty (value : String) : Property :=
  .data (.str value) true true true

private def addField (props : List (String × Property)) (pair : String × String) :
    List (String × Property) :=
  setOwn props pair.1 (literalDataProperty pair.2)

/-- Own properties after defining the string fields in source order. `setOwn`
keeps the first position of a key and replaces its value on a duplicate. -/
@[cbv_opaque] def literalProperties (fields : List (String × String)) : List (String × Property) :=
  fields.foldl addField []

/-- An ordinary object with those own data properties. -/
def literalRecord (fields : List (String × String)) : Obj :=
  { proto := some objectProto, props := literalProperties fields }

private theorem lookup_isSome (props : List (String × Property)) (key : String) :
    (props.lookup key).isSome = props.any (·.1 == key) := by
  induction props with
  | nil => rfl
  | cons p ps ih =>
    rcases p with ⟨k, v⟩
    by_cases h : key = k
    · simp [h]
    · simp [List.lookup_cons, beq_eq_false_iff_ne.mpr h, beq_eq_false_iff_ne.mpr (Ne.symm h), ih]

private theorem lookup_changed (props : List (String × Property)) (key search : String) (p : Property) :
    (props.map fun (k, v) => if k == key then (k, p) else (k, v)).lookup search =
      (props.lookup search).map (fun v => if search == key then p else v) := by
  induction props with
  | nil => rfl
  | cons a rest ih =>
    rcases a with ⟨k, v⟩
    by_cases h : search = k
    · subst k
      cases h' : search == key <;>
        simp only [List.map_cons, h', Bool.false_eq_true, ↓reduceIte, List.lookup_cons,
          beq_self_eq_true, Option.map_some]
    · have hfalse := beq_eq_false_iff_ne.mpr h
      cases h' : k == key <;>
        simp only [List.map_cons, h', Bool.false_eq_true, ↓reduceIte, List.lookup_cons, hfalse, ih]

private theorem lookup_setOwn (props : List (String × Property)) (key search : String) (p : Property) :
    (setOwn props key p).lookup search =
      if search == key then some p else props.lookup search := by
  rw [setOwn, ← lookup_isSome]
  cases h : props.lookup key with
  | none =>
    simp only [Option.isSome_none, Bool.false_eq_true, ite_false, List.lookup_append, List.lookup_cons,
      List.lookup_nil]
    by_cases h' : search = key
    · subst search
      simp [h]
    · simp [beq_eq_false_iff_ne.mpr h']
  | some val =>
    simp only [Option.isSome_some, ite_true, lookup_changed]
    by_cases h' : search = key
    · subst search
      simp [h]
    · simp [beq_eq_false_iff_ne.mpr h']

private theorem lookup_fields (fields : Fields) (props : List (String × Property)) (key : String) :
    ((fields.foldl addField props).lookup key) =
      (((fields.reverse).lookup key).map literalDataProperty).orElse (fun _ => props.lookup key) := by
  induction fields generalizing props with
  | nil => rfl
  | cons a rest ih =>
    rcases a with ⟨k, v⟩
    simp only [List.foldl_cons, List.reverse_cons, List.lookup_append, List.lookup_cons, List.lookup_nil]
    rw [ih]
    cases rest.reverse.lookup key <;> cases matchKey : key == k <;>
      simp only [Option.orElse, Option.or, Option.map, addField, lookup_setOwn, matchKey, Bool.false_eq_true,
        ↓reduceIte]

/-- The fallback after the concrete property equations. Keeping the underlying
value lookup separate lets indexed records also reduce an arbitrary computed key. -/
def lookupLiteralValue (fields : List (String × String)) (key : String) : Option String :=
  fields.reverse.lookup key

/-- A lookup sees the last definition of a field, or `none` for an absent key.
The certificate can register concrete field equations on this function. `cbv`
tries those equations before unfolding the fallback scan. -/
def lookupLiteralProperty (fields : List (String × String)) (key : String) : Option Property :=
  (lookupLiteralValue fields key).map literalDataProperty

namespace LiteralFieldIndex

/-- Quote a wide record as its ordered keys and a raw search tree. The linked AST check
compares the resulting properties, in order, to the actual program. The lookup
lemmas apply to any tree, including one with missing keys. -/
abbrev Tree := Std.DTreeMap.Internal.Impl String (fun _ => String)

def read (tree : Tree) (key : String) := Std.DTreeMap.Internal.Impl.Const.get? tree key

/-- A missing key contributes no field. Repeated keys retain their source positions. -/
def fields (tree : Tree) (keys : List String) : List (String × String) :=
  keys.filterMap fun k => (read tree k).map (k, ·)

theorem length_le (tree : Tree) (keys : List String) : (fields tree keys).length ≤ keys.length :=
  List.length_filterMap_le _ _

private theorem lookup (tree : Tree) (keys : List String) (key : String) :
    (fields tree keys).reverse.lookup key = if key ∈ keys then read tree key else none := by
  induction keys with
  | nil => simp [fields]
  | cons k ks ih =>
    by_cases h : key = k
    · subst k
      cases hv : read tree key with
      | none => simpa [fields, hv] using ih
      | some v =>
        simp only [fields, List.filterMap_cons, hv, Option.map_some, List.reverse_cons,
          List.lookup_append, List.lookup_cons, beq_self_eq_true, List.mem_cons_self, ite_true]
        change ((fields tree ks).reverse.lookup key).or (some v) = some v
        rw [ih, hv]
        split <;> rfl
    · cases hv : read tree k with
      | none => simpa [fields, hv, h] using ih
      | some v =>
        simpa [fields, hv, List.lookup_append, List.lookup_cons, h, beq_eq_false_iff_ne.mpr h] using ih

/-- A computed key need only search the index and check that a matching key
occurs in the source. It need not reconstruct every property's value. -/
theorem readValue (tree : Tree) (keys : List String) (key : String) :
    lookupLiteralValue (fields tree keys) key =
      (read tree key).bind fun value => if keys.contains key then some value else none := by
  by_cases h : key ∈ keys <;> cases hv : read tree key <;>
    simp [lookupLiteralValue, lookup, hv, h]

theorem hit {tree : Tree} {keys : List String} {key value : String} {i : Nat}
    (atIndex : keys[i]? = some key) (atKey : read tree key = some value) :
    lookupLiteralProperty (fields tree keys) key = some (literalDataProperty value) := by
  have mem := List.mem_of_getElem? atIndex
  simp [lookupLiteralProperty, lookupLiteralValue, lookup, mem, atKey]

theorem miss {tree : Tree} {keys : List String} {key : String} (atKey : read tree key = none) :
    lookupLiteralProperty (fields tree keys) key = none := by
  simp [lookupLiteralProperty, lookupLiteralValue, lookup, atKey]

end LiteralFieldIndex

theorem lookup_literalProperties (fields : List (String × String)) (key : String) :
    (literalProperties fields).lookup key = lookupLiteralProperty fields key := by
  simp [literalProperties, lookup_fields, lookupLiteralProperty, lookupLiteralValue]

private theorem modify_allocated (s : State W) (objs : Array Obj)
    (o : Obj) (f : Obj → Obj) :
    (s.heap.set! s.region (objs.push o)).modify s.region (·.modify objs.size f) =
      s.heap.set! s.region (objs.push (f o)) := by
  have inner : (objs.push o).modify objs.size f = objs.push (f o) := by
    apply Array.ext_getElem?
    intro i
    simp only [Array.getElem?_modify, Array.getElem?_push]
    by_cases hi : objs.size = i
    · subst i
      simp
    · simp [hi, Ne.symm hi]
  apply Array.ext_getElem?
  intro i
  simp only [Array.getElem?_modify, set!_getElem?]
  by_cases hi : s.region = i <;> simp [hi, inner]

private theorem alloc_field (previous : Fields) (k v : String) :
    ((do
      let a ← alloc (literalRecord previous)
      createData a k (.str v)
      pure a) : M W Addr) = alloc (literalRecord (previous ++ [(k, v)])) := by
  funext s
  cases hs : s.heap[s.region]? with
  | none => simp [bind, alloc, hs]
  | some objs =>
    let a : Addr := ⟨s.region, objs.size⟩
    let t := { s with heap := s.heap.set! s.region (objs.push (literalRecord previous)) }
    have ha : alloc (literalRecord previous) s = .ok a t := by simp [alloc, hs, a, t]
    change (alloc (literalRecord previous) >>= fun a => createData a k (.str v) >>= fun _ => pure a) s = _
    rw [bind_of_ok ha]
    have hg : getObj a t = .ok (literalRecord previous) t := by simp [getObj, alloc_objAt ha]
    unfold createData defineOwn
    rw [M.bind_assoc, bind_of_ok hg]
    have harray : (literalRecord previous).array = false := rfl
    simp only [harray, Bool.false_eq_true, ite_false]
    have hl : (literalRecord previous).props.lookup k = (previous.reverse.lookup k).map literalDataProperty :=
      lookup_literalProperties previous k
    rw [hl]
    have hd : applyDesc (((previous.reverse).lookup k).map literalDataProperty)
        { value := some (.str v), writable := some true, enumerable := some true, configurable := some true } =
        .ok (some (literalDataProperty v)) := by
      cases previous.reverse.lookup k <;> rfl
    rw [hd]
    change Res.ok a { t with heap := t.heap.modify a.region (·.modify a.index
      fun o => { o with props := setOwn o.props k (literalDataProperty v) }) } = _
    simp only [alloc, hs, t, a]
    congr 1
    simp only [modify_allocated]
    simp [literalRecord, literalProperties, addField, List.foldl_append]

private theorem eval_fields (n : Nat) (env : Env) (previous fields : Fields)
    (fuel : fields.length < n) :
    ((do
      let a ← alloc (literalRecord previous)
      evalProps n env a (fields.map literalField)
      pure (.obj a)) : M W Val) = (.obj <$> alloc (literalRecord (previous ++ fields))) := by
  induction fields generalizing previous n with
  | nil =>
    cases n with
    | zero => simp at fuel
    | succ n => rw [List.append_nil]; rfl
  | cons p rest ih =>
    rcases p with ⟨k, v⟩
    obtain ⟨n, rfl⟩ : ∃ m, n = m + 2 := by simp at fuel; exact ⟨n - 2, by omega⟩
    have runRest := ih (n + 1) (previous ++ [(k, v)]) (by simp at fuel; omega)
    have hkey : (toKey (.str k) : M W String) = pure k := rfl
    simp only [List.map_cons, literalField, evalProps, evalExpr, M.pure_bind, M.bind_assoc, hkey]
    change
      (alloc (literalRecord previous) >>= fun a => createData a k (.str v) >>= fun _ =>
        evalProps (n + 1) env a (rest.map literalField) >>= fun _ => pure (Val.obj a)) = _
    -- Keep the freshly allocated address across the initial property write.
    calc
      _ = (alloc (literalRecord previous) >>= fun a => createData a k (.str v) >>= fun _ => pure a) >>= fun a =>
          evalProps (n + 1) env a (rest.map literalField) >>= fun _ => pure (.obj a) := by
            simp [M.bind_assoc, M.pure_bind]
      _ = _ := by
        rw [alloc_field previous k v]
        simpa only [List.append_assoc, List.singleton_append] using runRest

/-- The static fields determine the exact newly allocated object. -/
theorem evalExpr_obj_string_fields (n : Nat) (env : Env) (fields : List (String × String))
    (fuel : fields.length < n) :
    (evalExpr (n + 1) env (.obj (fields.map literalField)) : M W Val) = .obj <$> alloc (literalRecord fields) := by
  rw [evalExpr]
  simpa [literalRecord, literalProperties] using eval_fields (W := W) n env [] fields fuel

end CoreJS
