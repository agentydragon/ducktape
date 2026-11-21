import Experiments.CoreJS.Eval

/-!
# CoreJS: an expression that only allocates changes nothing that existed

The first footprint rule proved against the model. `Expr.allocOnly` is the shape: literals, names, functions, object
literals with named data properties, array literals. `allocOnly_extends`: however such an expression ends, every
object and every binding that existed before is unchanged, and nothing is printed. What it made is new.
-/

namespace CoreJS

/-- `s'` has everything `s` has, unchanged: the objects, the bindings, the output. It may have more objects and bindings. -/
structure Extends (s s' : State) : Prop where
  objs : ∀ a o, s.objAt a = some o → s'.objAt a = some o
  cells : ∀ a b, s.cellAt a = some b → s'.cellAt a = some b
  out : s'.out = s.out
  region : s'.region = s.region

theorem Extends.refl (s : State) : Extends s s := ⟨fun _ _ h => h, fun _ _ h => h, rfl, rfl⟩

theorem Extends.trans {s t u : State} (h : Extends s t) (h' : Extends t u) : Extends s u :=
  ⟨fun a o ha => h'.objs a o (h.objs a o ha), fun a b ha => h'.cells a b (h.cells a b ha),
    h'.out.trans h.out, h'.region.trans h.region⟩

/-- An address that is free in `s` was free in everything `s` extends. -/
theorem Extends.fresh {base s : State} (h : Extends base s) {a : Addr} (ha : s.objAt a = none) : base.objAt a = none := by
  cases hb : base.objAt a with
  | none => rfl
  | some o => rw [h.objs a o hb] at ha; cases ha

theorem Extends.freshCell {base s : State} (h : Extends base s) {a : Addr} (ha : s.cellAt a = none) :
    base.cellAt a = none := by
  cases hb : base.cellAt a with
  | none => rfl
  | some o => rw [h.cells a o hb] at ha; cases ha

/-- However the computation ended, its state extends `base`. -/
def Res.Keeps {α : Type} (base : State) : Res α → Prop
  | .ok _ s | .throw _ s | .unknown _ s => Extends base s
  | .fuel => True

theorem keeps_bind {α β : Type} {m : M α} {f : α → M β} {base s : State} (hm : (m s).Keeps base)
    (hf : ∀ a s', m s = .ok a s' → Extends base s' → (f a s').Keeps base) : ((m >>= f) s).Keeps base := by
  show (match m s with
    | .ok a s' => f a s'
    | .throw v s' => .throw v s'
    | .unknown why s' => .unknown why s'
    | .fuel => .fuel : Res β).Keeps base
  cases h : m s with
  | ok a s' => rw [h] at hm; exact hf a s' h hm
  | throw v s' => rw [h] at hm; exact hm
  | unknown why s' => rw [h] at hm; exact hm
  | fuel => trivial

/-- What a sequence ended well with, step by step. -/
theorem bind_ok {α β : Type} {m : M α} {f : α → M β} {s s' : State} {b : β} (h : (m >>= f) s = .ok b s') :
    ∃ a t, m s = .ok a t ∧ f a t = .ok b s' := by
  have h' : (match m s with
    | .ok a s' => f a s'
    | .throw v s' => .throw v s'
    | .unknown why s' => .unknown why s'
    | .fuel => .fuel : Res β) = .ok b s' := h
  cases hm : m s with
  | ok a t => rw [hm] at h'; exact ⟨a, t, rfl, h'⟩
  | throw v t => rw [hm] at h'; cases h'
  | unknown why t => rw [hm] at h'; cases h'
  | fuel => rw [hm] at h'; cases h'

/-- A computation that reads the state and leaves it as it is. -/
def Inert {α : Type} (m : M α) : Prop :=
  ∀ s, match m s with
    | .ok _ s' | .throw _ s' | .unknown _ s' => s' = s
    | .fuel => True

theorem Inert.keeps {α : Type} {m : M α} (h : Inert m) {base s : State} (hb : Extends base s) : (m s).Keeps base := by
  have := h s
  cases hm : m s <;> simp_all [Res.Keeps]

theorem Inert.ok {α : Type} {m : M α} (h : Inert m) {s s' : State} {a : α} (hm : m s = .ok a s') : s' = s := by
  have := h s
  rw [hm] at this
  exact this

theorem inert_pure {α : Type} (a : α) : Inert (pure a : M α) := fun _ => rfl
theorem inert_getState : Inert getState := fun _ => rfl
theorem inert_throwVal {α : Type} (v : Val) : Inert (throwVal v : M α) := fun _ => rfl
theorem inert_throwErr {α : Type} (k : ErrorKind) (msg : String) : Inert (throwErr k msg : M α) := fun _ => rfl
theorem inert_unknown {α : Type} (why : String) : Inert (unknown why : M α) := fun _ => rfl

theorem inert_lift {α : Type} (x : Except Abort α) : Inert (lift x) := by
  intro s
  rcases x with (_ | _) | _ <;> rfl

theorem inert_bind {α β : Type} {m : M α} {f : α → M β} (hm : Inert m) (hf : ∀ a, Inert (f a)) : Inert (m >>= f) := by
  intro s
  show match (match m s with
    | .ok a s' => f a s'
    | .throw v s' => .throw v s'
    | .unknown why s' => .unknown why s'
    | .fuel => .fuel : Res β) with
    | .ok _ s' | .throw _ s' | .unknown _ s' => s' = s
    | .fuel => True
  have h := hm s
  cases hms : m s with
  | ok a s' =>
    rw [hms] at h
    subst h
    exact hf a s'
  | throw v s' => rw [hms] at h; exact h
  | unknown why s' => rw [hms] at h; exact h
  | fuel => trivial

theorem inert_getObj (a : Addr) : Inert (getObj a) := by
  intro s
  unfold getObj
  cases s.objAt a <;> rfl

theorem inert_readVar (env : Env) (x : Name) : Inert (readVar env x) := by
  unfold readVar
  split
  · exact inert_throwErr _ _
  · refine inert_bind inert_getState fun s => ?_
    split
    · exact inert_pure _
    · exact inert_throwErr _ _

/-! ## The steps that change the state -/

theorem objAt_push {s : State} {objs : Array Obj} (h : s.heap[s.region]? = some objs) (o : Obj) (a : Addr) :
    ({ s with heap := s.heap.set! s.region (objs.push o) } : State).objAt a =
      if a = ⟨s.region, objs.size⟩ then some o else s.objAt a := by
  have hlt : s.region < s.heap.size := by
    rcases Nat.lt_or_ge s.region s.heap.size with h' | h'
    · exact h'
    · rw [Array.getElem?_eq_none h'] at h; cases h
  rcases a with ⟨r, i⟩
  by_cases hr : r = s.region
  · subst hr
    have heq : s.heap[s.region] = objs := by simpa [Array.getElem?_eq_getElem hlt] using h
    by_cases hi : i = objs.size
    · subst hi; simp [State.objAt, hlt]
    · simp [State.objAt, hlt, hi, Array.getElem?_push, heq]
  · have hne : ¬ s.region = r := fun h' => hr h'.symm
    simp [State.objAt, hr, hne]

theorem cellAt_push {s : State} {bs : Array Binding} (h : s.cells[s.region]? = some bs) (b : Binding) (a : Addr) :
    ({ s with cells := s.cells.set! s.region (bs.push b) } : State).cellAt a =
      if a = ⟨s.region, bs.size⟩ then some b else s.cellAt a := by
  have hlt : s.region < s.cells.size := by
    rcases Nat.lt_or_ge s.region s.cells.size with h' | h'
    · exact h'
    · rw [Array.getElem?_eq_none h'] at h; cases h
  rcases a with ⟨r, i⟩
  by_cases hr : r = s.region
  · subst hr
    have heq : s.cells[s.region] = bs := by simpa [Array.getElem?_eq_getElem hlt] using h
    by_cases hi : i = bs.size
    · subst hi; simp [State.cellAt, hlt]
    · simp [State.cellAt, hlt, hi, Array.getElem?_push, heq]
  · have hne : ¬ s.region = r := fun h' => hr h'.symm
    simp [State.cellAt, hr, hne]

theorem objAt_modify (s : State) (a : Addr) (f : Obj → Obj) (a' : Addr) (h : a' ≠ a) :
    ({ s with heap := s.heap.modify a.region (·.modify a.index f) } : State).objAt a' = s.objAt a' := by
  rcases a with ⟨r, i⟩
  rcases a' with ⟨r', i'⟩
  by_cases hr : r = r'
  · subst hr
    have hi : ¬ i = i' := fun h' => h (by rw [h'])
    simp only [State.objAt, Array.getElem?_modify, ite_true]
    cases s.heap[r]? <;> simp [Array.getElem?_modify, hi]
  · simp [State.objAt, Array.getElem?_modify, hr]

theorem cellAt_modify (s : State) (a : Addr) (f : Binding → Binding) (a' : Addr) (h : a' ≠ a) :
    ({ s with cells := s.cells.modify a.region (·.modify a.index f) } : State).cellAt a' = s.cellAt a' := by
  rcases a with ⟨r, i⟩
  rcases a' with ⟨r', i'⟩
  by_cases hr : r = r'
  · subst hr
    have hi : ¬ i = i' := fun h' => h (by rw [h'])
    simp only [State.cellAt, Array.getElem?_modify, ite_true]
    cases s.cells[r]? <;> simp [Array.getElem?_modify, hi]
  · simp [State.cellAt, Array.getElem?_modify, hr]

/-- `alloc` gives an address that was free, and extends the state. -/
theorem alloc_ok {o : Obj} {s s' : State} {a : Addr} (h : alloc o s = .ok a s') : s.objAt a = none ∧ Extends s s' := by
  unfold alloc at h
  split at h
  · rename_i objs hobjs
    cases h
    refine ⟨by simp [State.objAt, hobjs], fun a' o' ha' => ?_, fun _ _ hc => hc, rfl, rfl⟩
    rw [objAt_push hobjs]
    split
    · rename_i heq
      subst heq
      simp [State.objAt, hobjs] at ha'
    · exact ha'
  · cases h

theorem alloc_keeps (o : Obj) {base s : State} (hb : Extends base s) : (alloc o s).Keeps base := by
  cases h : alloc o s with
  | ok a s' => exact hb.trans (alloc_ok h).2
  | throw v s' => unfold alloc at h; split at h <;> cases h
  | unknown why s' => unfold alloc at h; split at h <;> cases h; exact hb
  | fuel => trivial

theorem newCell_ok {b : Binding} {s s' : State} {a : Addr} (h : newCell b s = .ok a s') :
    s.cellAt a = none ∧ Extends s s' := by
  unfold newCell at h
  split at h
  · rename_i bs hbs
    cases h
    refine ⟨by simp [State.cellAt, hbs], fun _ _ ho => ho, fun a' b' ha' => ?_, rfl, rfl⟩
    rw [cellAt_push hbs]
    split
    · rename_i heq
      subst heq
      simp [State.cellAt, hbs] at ha'
    · exact ha'
  · cases h

/-- A change to an object that `base` does not have keeps `base`. -/
theorem modifyObj_keeps (a : Addr) (f : Obj → Obj) {base s : State} (hb : Extends base s) (ha : base.objAt a = none) :
    (modifyObj a f s).Keeps base := by
  refine ⟨fun a' o h => ?_, hb.cells, hb.out, hb.region⟩
  have hne : a' ≠ a := fun heq => by rw [heq, ha] at h; cases h
  show State.objAt _ a' = some o
  rw [objAt_modify s a f a' hne]
  exact hb.objs a' o h

theorem setCell_keeps (a : Addr) (v : Val) {base s : State} (hb : Extends base s) (ha : base.cellAt a = none) :
    (setCell a v s).Keeps base := by
  refine ⟨hb.objs, fun a' b h => ?_, hb.out, hb.region⟩
  have hne : a' ≠ a := fun heq => by rw [heq, ha] at h; cases h
  show State.cellAt _ a' = some b
  rw [cellAt_modify s a _ a' hne]
  exact hb.cells a' b h

theorem newCell_keeps (b : Binding) {base s : State} (hb : Extends base s) : (newCell b s).Keeps base := by
  cases h : newCell b s with
  | ok a s' => exact hb.trans (newCell_ok h).2
  | throw v s' => unfold newCell at h; split at h <;> cases h
  | unknown why s' => unfold newCell at h; split at h <;> cases h; exact hb
  | fuel => trivial

theorem declare_keeps (env : Env) (bs : List (Name × Binding)) {base s : State} (hb : Extends base s) :
    (declare env bs s).Keeps base := by
  induction bs generalizing s with
  | nil => exact (inert_pure _).keeps hb
  | cons b rest ih =>
    obtain ⟨x, b⟩ := b
    unfold declare
    refine keeps_bind (newCell_keeps b hb) fun a s1 _ hb1 => ?_
    refine keeps_bind (ih hb1) fun env1 s2 _ hb2 => ?_
    exact (inert_pure _).keeps hb2

/-- One declared name is first in the new scope, at an address that was free. -/
theorem declare_one {env env' : Env} {x : Name} {b : Binding} {s s' : State}
    (h : declare env [(x, b)] s = .ok env' s') : ∃ c, env'.vars.lookup x = some c ∧ s.cellAt c = none := by
  unfold declare at h
  obtain ⟨c, t, hc, h⟩ := bind_ok h
  unfold declare at h
  cases h
  exact ⟨c, by simp [List.lookup], (newCell_ok hc).1⟩

theorem initVar_keeps {env : Env} {x : Name} {c : Addr} (v : Val) {base s : State} (hb : Extends base s)
    (hx : env.vars.lookup x = some c) (hc : base.cellAt c = none) : (initVar env x v s).Keeps base := by
  unfold initVar
  rw [hx]
  exact setCell_keeps c v hb hc

theorem selfScope_keeps (env : Env) (self : Bool) (x : Name) {base s : State} (hb : Extends base s) :
    (selfScope env self x s).Keeps base := by
  unfold selfScope
  split
  · exact declare_keeps _ _ hb
  · exact (inert_pure _).keeps hb

/-- Giving a function or a class its own name writes a binding that `selfScope` made, so one `base` does not have. -/
theorem initSelf_keeps {env env1 : Env} {self : Bool} {x : Name} (v : Val) {base s0 s1 s : State}
    (h : selfScope env self x s0 = .ok env1 s1) (h0 : Extends base s0) (hb : Extends base s) :
    (initSelf env1 self x v s).Keeps base := by
  unfold initSelf
  split
  · rename_i hs
    unfold selfScope at h
    simp only [hs, ite_true] at h
    obtain ⟨c, hc, hfree⟩ := declare_one h
    exact initVar_keeps v hb hc (h0.freshCell hfree)
  · exact (inert_pure _).keeps hb

theorem addPrototype_keeps (a : Addr) {base s : State} (hb : Extends base s) (ha : base.objAt a = none) :
    (addPrototype a s).Keeps base := by
  unfold addPrototype
  exact keeps_bind (alloc_keeps _ hb) fun p s' _ hb' => modifyObj_keeps a _ hb' ha

theorem makeFunc_keeps (env : Env) (f : Func) {base s : State} (hb : Extends base s) : (makeFunc env f s).Keeps base := by
  unfold makeFunc
  refine keeps_bind (selfScope_keeps _ _ _ hb) fun env1 s1 h1 hb1 => ?_
  refine keeps_bind (alloc_keeps _ hb1) fun a s2 h2 hb2 => ?_
  have ha : base.objAt a = none := hb1.fresh (alloc_ok h2).1
  refine keeps_bind ?_ fun _ s3 _ hb3 => ?_
  · split
    · exact addPrototype_keeps a hb2 ha
    · exact (inert_pure _).keeps hb2
  · exact keeps_bind (initSelf_keeps _ h1 hb hb3) fun _ s4 _ hb4 => (inert_pure _).keeps hb4

/-- A property defined on an object that `base` does not have. -/
theorem defineOwn_keeps (a : Addr) (k : String) (d : Desc) {base s : State} (hb : Extends base s)
    (ha : base.objAt a = none) : (defineOwn a k d s).Keeps base := by
  unfold defineOwn
  refine keeps_bind ((inert_getObj a).keeps hb) fun o s1 _ hb1 => ?_
  split
  · exact (inert_unknown _).keeps hb1
  · refine keeps_bind ((inert_lift _).keeps hb1) fun r s2 _ hb2 => ?_
    split
    · exact modifyObj_keeps a _ hb2 ha
    · exact (inert_throwErr _ _).keeps hb2

/-! ## The shape, and the theorem -/

mutual
/-- An expression that only allocates: a literal, a name, a function, an object literal of named data properties or an array literal of such expressions. -/
def Expr.allocOnly : Expr → Bool
  | .undef | .null | .bool _ | .num _ | .str _ | .var _ | .host _ | .this | .func _ => true
  | .obj props => allocOnlyProps props
  | .arr elems => allocOnlyAll elems
  | _ => false

def allocOnlyAll : List Expr → Bool
  | [] => true
  | e :: es => e.allocOnly && allocOnlyAll es

def allocOnlyProps : List PropDef → Bool
  | [] => true
  | .init (.str _) v :: ps => v.allocOnly && allocOnlyProps ps
  | _ :: _ => false
end

theorem allocOnly_keeps (n : Nat) :
    (∀ env e base s, e.allocOnly = true → Extends base s → (evalExpr n env e s).Keeps base) ∧
    (∀ env es base s, allocOnlyAll es = true → Extends base s → (evalArgs n env es s).Keeps base) ∧
    (∀ env a ps base s, allocOnlyProps ps = true → Extends base s → base.objAt a = none →
      (evalProps n env a ps s).Keeps base) := by
  induction n with
  | zero =>
    refine ⟨fun env e base s _ _ => ?_, fun env es base s _ _ => ?_, fun env a ps base s _ _ _ => ?_⟩
    · unfold evalExpr; trivial
    · unfold evalArgs; trivial
    · unfold evalProps; trivial
  | succ n ih =>
    obtain ⟨ihE, ihA, ihP⟩ := ih
    refine ⟨fun env e base s he hb => ?_, fun env es base s he hb => ?_, fun env a ps base s he hb ha => ?_⟩
    · cases e <;> simp only [Expr.allocOnly, Bool.false_eq_true] at he <;> unfold evalExpr
      case undef | null | bool | num | str | host | this => exact (inert_pure _).keeps hb
      case var x => exact (inert_readVar env x).keeps hb
      case func f => exact makeFunc_keeps env f hb
      case obj props =>
        refine keeps_bind (alloc_keeps _ hb) fun a s1 h1 hb1 => ?_
        refine keeps_bind (ihP env a props base s1 he hb1 (hb.fresh (alloc_ok h1).1)) fun _ s2 _ hb2 => ?_
        exact (inert_pure _).keeps hb2
      case arr elems =>
        refine keeps_bind (ihA env elems base s he hb) fun vs s1 _ hb1 => ?_
        refine keeps_bind (alloc_keeps _ hb1) fun a s2 _ hb2 => ?_
        exact (inert_pure _).keeps hb2
    · cases es with
      | nil => unfold evalArgs; exact (inert_pure _).keeps hb
      | cons e es =>
        simp only [allocOnlyAll, Bool.and_eq_true] at he
        unfold evalArgs
        refine keeps_bind (ihE env e base s he.1 hb) fun v s1 _ hb1 => ?_
        refine keeps_bind (ihA env es base s1 he.2 hb1) fun vs s2 _ hb2 => ?_
        exact (inert_pure _).keeps hb2
    · cases ps with
      | nil => unfold evalProps; exact (inert_pure _).keeps hb
      | cons p ps =>
        cases p with
        | init k v =>
          cases k <;> simp only [allocOnlyProps, Bool.and_eq_true, Bool.false_eq_true] at he
          rename_i key
          unfold evalProps
          refine keeps_bind (ihE env (.str key) base s rfl hb) fun kv s1 _ hb1 => ?_
          refine keeps_bind ((inert_lift _).keeps hb1) fun name s2 _ hb2 => ?_
          refine keeps_bind (ihE env v base s2 he.1 hb2) fun vv s3 _ hb3 => ?_
          exact keeps_bind (defineOwn_keeps a name _ hb3 ha) fun _ s4 _ hb4 => ihP env a ps base s4 he.2 hb4 ha
        | getter | setter | proto => simp only [allocOnlyProps, Bool.false_eq_true] at he

/-- **An expression that only allocates changes nothing that existed**, however it ends: with a value, with a throw
(a name in its dead zone), or out of fuel. Every object and binding of `s` is in the final state unchanged, and the
output is the same. -/
theorem allocOnly_extends {e : Expr} (h : e.allocOnly = true) (fuel : Nat) (env : Env) (s : State) :
    (evalExpr fuel env e s).Keeps s :=
  (allocOnly_keeps fuel).1 env e s s h (Extends.refl s)

/-! ## A declaration whose initializer only allocates binds its name and nothing else -/

/-- As `Extends`, but the binding at `c` may have a new value. -/
structure ExtendsBut (c : Option Addr) (s s' : State) : Prop where
  objs : ∀ a o, s.objAt a = some o → s'.objAt a = some o
  cells : ∀ a b, some a ≠ c → s.cellAt a = some b → s'.cellAt a = some b
  out : s'.out = s.out
  region : s'.region = s.region

theorem Extends.but {s s' : State} (h : Extends s s') (c : Option Addr) : ExtendsBut c s s' :=
  ⟨h.objs, fun a b _ => h.cells a b, h.out, h.region⟩

def Res.KeepsBut {α : Type} (c : Option Addr) (base : State) : Res α → Prop
  | .ok _ s | .throw _ s | .unknown _ s => ExtendsBut c base s
  | .fuel => True

theorem Inert.keepsBut {α : Type} {m : M α} (h : Inert m) (c : Option Addr) {base s : State} (hb : Extends base s) :
    (m s).KeepsBut c base := by
  have := h.keeps hb
  cases hm : m s <;> rw [hm] at this <;> first | exact Extends.but this c | trivial

/-- A step that keeps all but `c`, then steps that change nothing. -/
theorem keepsBut_bind_inert {α β : Type} {m : M α} {f : α → M β} {c : Option Addr} {base s : State}
    (hm : (m s).KeepsBut c base) (hf : ∀ a, Inert (f a)) : ((m >>= f) s).KeepsBut c base := by
  show (match m s with
    | .ok a s' => f a s'
    | .throw v s' => .throw v s'
    | .unknown why s' => .unknown why s'
    | .fuel => .fuel : Res β).KeepsBut c base
  cases h : m s with
  | ok a s' =>
    rw [h] at hm
    have hi := hf a s'
    show (f a s').KeepsBut c base
    cases hfa : f a s' <;> rw [hfa] at hi <;> first | (subst hi; exact hm) | trivial
  | throw v s' => rw [h] at hm; exact hm
  | unknown why s' => rw [h] at hm; exact hm
  | fuel => trivial

/-- Steps that keep everything, then one that keeps all but `c`. -/
theorem keeps_bind_but {α β : Type} {m : M α} {f : α → M β} {c : Option Addr} {base s : State}
    (hm : (m s).Keeps base) (hf : ∀ a s', Extends base s' → (f a s').KeepsBut c base) :
    ((m >>= f) s).KeepsBut c base := by
  show (match m s with
    | .ok a s' => f a s'
    | .throw v s' => .throw v s'
    | .unknown why s' => .unknown why s'
    | .fuel => .fuel : Res β).KeepsBut c base
  cases h : m s with
  | ok a s' => rw [h] at hm; exact hf a s' hm
  | throw v s' => rw [h] at hm; exact hm.but c
  | unknown why s' => rw [h] at hm; exact hm.but c
  | fuel => trivial

theorem setCell_keepsBut (c : Addr) (v : Val) {base s : State} (hb : Extends base s) :
    (setCell c v s).KeepsBut (some c) base := by
  refine ⟨hb.objs, fun a b hne h => ?_, hb.out, hb.region⟩
  have hne' : a ≠ c := fun heq => hne (by rw [heq])
  show State.cellAt _ a = some b
  rw [cellAt_modify s c _ a hne']
  exact hb.cells a b h

theorem initVar_keepsBut (env : Env) (x : Name) (v : Val) {base s : State} (hb : Extends base s) :
    (initVar env x v s).KeepsBut (env.vars.lookup x) base := by
  unfold initVar
  cases env.vars.lookup x with
  | none => exact (inert_unknown _).keepsBut none hb
  | some c => exact setCell_keepsBut c v hb

theorem writeVar_keepsBut (env : Env) (x : Name) (v : Val) {base s : State} (hb : Extends base s) :
    (writeVar env x v s).KeepsBut (env.vars.lookup x) base := by
  unfold writeVar
  cases env.vars.lookup x with
  | none => exact (inert_throwErr _ _).keepsBut none hb
  | some c =>
    show (match s.cellAt c with
      | some { value := some _, mutable := true } => setCell c v
      | some { value := some _, mutable := false } => throwErr .type "assignment to constant variable"
      | _ => throwErr .reference s!"cannot access '{x}' before initialization" : M Unit) s |>.KeepsBut (some c) base
    generalize s.cellAt c = cell
    rcases cell with _ | ⟨_ | _, _ | _⟩
    · exact (inert_throwErr _ _).keepsBut _ hb
    · exact (inert_throwErr _ _).keepsBut _ hb
    · exact (inert_throwErr _ _).keepsBut _ hb
    · exact (inert_throwErr _ _).keepsBut _ hb
    · exact setCell_keepsBut c v hb

/-- **A declaration whose initializer only allocates** leaves every object that existed as it was, prints nothing, and
changes no binding but the one of its name. This is the footprint "binds `x`, no effect" for such a unit. -/
theorem decl_allocOnly {e : Expr} (h : e.allocOnly = true) (fuel : Nat) (env : Env) (kind : DeclKind) (x : Name)
    (s : State) : (execStmt fuel env (.decl kind x e) s).KeepsBut (env.vars.lookup x) s := by
  cases fuel with
  | zero => unfold execStmt; trivial
  | succ n =>
    cases kind <;> unfold execStmt
    case var =>
      refine keeps_bind_but (allocOnly_extends h n env s) fun v s1 hb1 => ?_
      exact keepsBut_bind_inert (writeVar_keepsBut env x v hb1) fun _ => inert_pure _
    all_goals
      refine keeps_bind_but (allocOnly_extends h n env s) fun v s1 hb1 => ?_
      exact keepsBut_bind_inert (initVar_keepsBut env x v hb1) fun _ => inert_pure _

/-! ## Two such declarations: the statement, not proved

`Extends` says what one unit leaves alone. That two units commute needs one more fact, that what an expression of this
shape computes depends only on the bindings it reads, and then the two writes and the two sets of new objects have to
be exchanged. The second step is where the address of an allocation matters. A unit allocates in its own region, so
its new objects get the same addresses whichever unit ran first, and the claim below is an equality of states. With
one shared counter it would hold only up to a renaming of addresses, and "the same state" would need a definition.
-/

mutual
/-- The names an expression that only allocates reads when it runs. A function reads none: it keeps bindings, not values. -/
def Expr.readsNow : Expr → List Name
  | .var x => [x]
  | .obj props => readsNowProps props
  | .arr elems => readsNowAll elems
  | _ => []

def readsNowAll : List Expr → List Name
  | [] => []
  | e :: es => e.readsNow ++ readsNowAll es

def readsNowProps : List PropDef → List Name
  | [] => []
  | .init _ v :: ps => v.readsNow ++ readsNowProps ps
  | _ :: ps => readsNowProps ps
end

/-- Units `i` and `j` declare different bindings by expressions that only allocate, and neither reads the binding the
other declares. Then if `i` then `j` ends well, `j` then `i` ends well in the same state. -/
def AllocOnlyUnitsCommute : Prop :=
  ∀ (fuel : Nat) (env : Env) (ss : List Stmt) (i j : Nat) (ki kj : DeclKind) (x y : Name) (e e' : Expr) (s s' : State),
    ss[i]? = some (.decl ki x e) → ss[j]? = some (.decl kj y e') → i ≠ j →
    e.allocOnly = true → e'.allocOnly = true → env.vars.lookup x ≠ env.vars.lookup y →
    (∀ z ∈ e.readsNow, env.vars.lookup z ≠ env.vars.lookup y) →
    (∀ z ∈ e'.readsNow, env.vars.lookup z ≠ env.vars.lookup x) →
    (runUnit fuel env ss i >>= fun _ => runUnit fuel env ss j) s = .ok .normal s' →
    (runUnit fuel env ss j >>= fun _ => runUnit fuel env ss i) s = .ok .normal s'

#print axioms allocOnly_extends
#print axioms decl_allocOnly

end CoreJS
