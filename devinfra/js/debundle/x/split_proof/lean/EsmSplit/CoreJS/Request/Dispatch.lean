import EsmSplit.CoreJS.InvocationFrame
import EsmSplit.CoreJS.Request.Allocation

/-! Frame the CoreJS primitive operations and every request the opaque world can issue. -/

namespace CoreJS

variable {W : World}

private theorem reachFrame_logLine {r R cells A} (line : List String) :
    ReachFrame r R cells A (SafeVal A) (do
      modifyState fun s => { s with out := s.out.push (" ".intercalate line) }
      pure Val.undef : M W Val) := by
  constructor
  · intros
    rfl
  · intro s _ _
    exact ⟨rfl, rfl, fun _ _ => rfl, rfl⟩
  · intro s hs _
    exact ⟨(fun _ h => by cases h), hs.handed, hs.supplied, fun n b hn edge => by
      cases edge with
      | obj ho hm => exact hs.closed _ _ hn (.obj ho hm)
      | cell hc hm => exact hs.closed _ _ hn (.cell hc hm)⟩

theorem reachFrame_prim_log {r R cells A} (call : Call W) (args : List Val) :
    ReachFrame r R cells A (SafeVal A) (prim call .log args) := by
  simp only [prim]
  have rest : ReachFrame r R cells A (SafeVal A) (do
      let line ← lift (args.mapM logArg)
      modifyState fun s => { s with out := s.out.push (" ".intercalate line) }
      pure Val.undef : M W Val) :=
    (reachFrame_lift (r := r) (R := R) (cells := cells) (A := A) (args.mapM logArg)
      (P := fun _ => True) fun _ _ => trivial).bind fun line _ => reachFrame_logLine line
  split
  · rename_i format head tail
    split
    · exact reachFrame_unknown r R cells A _ _
    · exact rest
  · exact rest


private theorem safe_arg {A : HeapNode → Prop} {args : List Val} (safe : ∀ x ∈ args, SafeVal A x) (i : Nat) :
    SafeVal A (args.getD i .undef) := by
  rw [List.getD_eq_getElem?_getD]
  cases h : args[i]? with
  | none => exact fun _ hn => by cases hn
  | some x => exact safe x (list_index_mem h)

private theorem reachFrame_readRegExp {r R cells A} (away : Away r R A) (call : Call W)
    (hf : CallFramed r R cells A call) (root : A (.obj objectProto))
    (getter : RegExpGetter) (v : Val) (safe : SafeVal A v) :
    ReachFrame r R cells A (SafeVal A) (readRegExp call getter v) := by
  unfold readRegExp
  apply (reachFrame_requireObj _ v safe).bind
  intro a ha
  have slot (letter : Option Char) : ReachFrame r R cells A (SafeVal A) (do
      regexpSlot a (← getObj a).regexp letter : M W Val) := by
    apply (reachFrame_getObj away ha).bind
    intro o _
    unfold regexpSlot
    cases o.regexp <;> cases letter <;> simp only <;> repeat' split
    all_goals first
      | exact reachFrame_pure r R cells A _ (fun _ hn => by cases hn)
      | exact reachFrame_unknown r R cells A _ _
      | exact reachFrame_typeError _
  cases getter with
  | source => exact slot none
  | flag c => exact slot (some c)
  | flags =>
    have flags (keys : List (String × String)) :
        ReachFrame r R cells A (fun _ => True) (regexpFlags call v keys) := by
      induction keys with
      | nil => exact reachFrame_pure r R cells A _ trivial
      | cons pair rest ih =>
        rcases pair with ⟨key, letter⟩
        apply (reachFrame_getProp away call hf.getter root v (.str key) safe).bind
        intro value _
        apply (reachFrame_lift (P := fun _ => True) (toBoolean value) (fun _ _ => trivial)).bind
        intro enabled _
        exact ih.bind fun tail _ => reachFrame_pure r R cells A _ trivial
    exact (flags regexpFlagOrder).bind fun text _ =>
      reachFrame_pure r R cells A (P := SafeVal A) (Val.str text) (fun _ hn => by cases hn)

private theorem reachFrame_forInLevels {r R cells A} (away : Away r R A) (n : Nat)
    (a : Addr) (address : A (.obj a)) :
    ReachFrame r R cells A (fun _ => True) (forInLevels n a : M W _) := by
  induction n generalizing a with
  | zero => exact reachFrame_unknown r R cells A _ _
  | succ n ih =>
    simp only [forInLevels]
    apply (reachFrame_getObj away address).bind
    intro obj safe
    split
    · exact reachFrame_unknown r R cells A _ _
    · cases parent : obj.proto with
      | none =>
        simp only [M.pure_bind]
        exact reachFrame_pure r R cells A _ trivial
      | some b =>
        have aParent : A (.obj b) := safe _ (by simp [Obj.nodes, parent])
        exact (ih b aParent).bind fun _ _ => reachFrame_pure r R cells A _ trivial

private theorem reachFrame_forInBaseLevels {r R cells A} (away : Away r R A) (base : Val) (safe : SafeVal A base) :
    ReachFrame r R cells A (fun _ => True) (forInBaseLevels base : M W _) := by
  cases base <;> first
    | exact reachFrame_pure r R cells A [] trivial
    | exact reachFrame_forInLevels away 32 _ (safeVal_obj.1 safe)
    | exact reachFrame_unknown r R cells A _ _

private theorem reachFrame_forInKeys {r R cells A} (away : Away r R A)
    (objects : ∀ a : Addr, a.region ≠ r → A (.obj a)) (base : Val) (safe : SafeVal A base) :
    ReachFrame r R cells A (SafeVal A) (forInKeys base : M W Val) := by
  unfold forInKeys
  apply (reachFrame_forInBaseLevels away base safe).bind
  intro levels _
  apply (reachFrame_alloc _ ?_ objects).bind
    fun addr address => reachFrame_pure r R cells A (Val.obj addr) (safeVal_obj.2 address)
  simp [forInKeyTable, Obj.nodes, Property.nodes, Val.nodes]
  intro _ _ property _ _ _ _ equality member
  subst property
  simp at member

private theorem reachFrame_checkForInWrite {r R cells A} (away : Away r R A)
    (base target key : Val) (baseSafe : SafeVal A base) (targetSafe : SafeVal A target) :
    ReachFrame r R cells A (SafeVal A) (checkForInWrite base target key : M W Val) := by
  cases base <;> try exact reachFrame_unknown r R cells A _ _
  case obj source =>
    cases target <;> try exact reachFrame_unknown r R cells A _ _
    case obj destination =>
      unfold checkForInWrite
      apply (reachFrame_forInLevels away 32 source (safeVal_obj.1 baseSafe)).bind
      intro levels _
      split
      · exact reachFrame_unknown r R cells A _ _
      · apply (reachFrame_forInLevels away 32 destination (safeVal_obj.1 targetSafe)).bind
        intro _ _
        apply (reachFrame_toKey r R cells A key).bind
        intro k _
        apply (reachFrame_lookupProp away (safeVal_obj.1 targetSafe) k).bind
        intro property _
        split
        · exact reachFrame_unknown r R cells A _ _
        · exact reachFrame_pure r R cells A Val.undef (by simp [SafeVal, Val.nodes])

private theorem reachFrame_privateSlot {r R cells A} (away : Away r R A)
    (op : PrivateOp) (table receiver value : Val) (ht : SafeVal A table)
    (hr : SafeVal A receiver) (hv : SafeVal A value) :
    ReachFrame r R cells A (SafeVal A) (privateSlot op table receiver value : M W Val) := by
  unfold privateSlot
  apply (reachFrame_requireObj "private name table" table ht).bind
  intro table ha
  apply (reachFrame_requireObj "private member receiver" receiver hr).bind
  intro receiver _
  apply (reachFrame_lookupProp away ha (.symbol receiver)).bind
  intro present hp
  have dataSafe : ∀ v, v ∈ some value → SafeVal A v := by
    intro v h
    cases h
    exact hv
  have noneSafe : ∀ v, v ∈ (none : Option Val) → SafeVal A v := by
    intro v h
    cases h
  have save (w : Option Bool) : ReachFrame r R cells A (SafeVal A)
      (do defineOwn table (.symbol receiver) { value := some value, writable := w }; pure value : M W Val) :=
    (reachFrame_defineOwn away ha (.symbol receiver) { value := some value, writable := w }
      ⟨dataSafe, noneSafe, noneSafe⟩).bind fun _ _ =>
        reachFrame_pure r R cells A value hv
  cases op <;> cases present <;> try exact reachFrame_throwErr r R cells A _ _ _
  all_goals first
    | exact save _
    | rename_i property; cases property <;> try exact reachFrame_throwErr r R cells A _ _ _
  · exact reachFrame_pure r R cells A _ (hp _ rfl)
  · rename_i v writable enumerable configurable
    cases writable
    · exact reachFrame_throwErr r R cells A _ _ _
    · exact save none

theorem reachFrame_prim {r R cells A} (away : Away r R A) (call : Call W) (hf : CallFramed r R cells A call)
    (objects : ∀ a : Addr, a.region ≠ r → A (.obj a)) (root : A (.obj objectProto))
    (p : Prim) (args : List Val) (safe : ∀ x ∈ args, SafeVal A x) :
    ReachFrame r R cells A (SafeVal A) (prim call p args) := by
  cases p with
  | regExpGet getter => exact reachFrame_readRegExp away call hf root getter _ (safe_arg safe 0)
  | globalTypeof name => exact reachFrame_observeGlobalTypeof r R cells A name
  | globalRef name => exact reachFrame_readHostRef r R cells A (.binding name)
  | wellKnownSymbol sym =>
    apply (reachFrame_readHostRef r R cells A (.property "Symbol" sym.member)).bind
    intro value _
    split
    · exact reachFrame_pure r R cells A (P := SafeVal A) (.wellKnownSymbol sym) (by simp [SafeVal, Val.nodes])
    · exact reachFrame_unknown r R cells A _ _
  | freshSymbol =>
    apply (reachFrame_readHostRef r R cells A (.binding "Symbol")).bind
    intro value _
    have fresh : ReachFrame r R cells A (SafeVal A)
        (Val.symbol <$> alloc { proto := none, props := [] } : M W Val) :=
      (reachFrame_alloc _ (by simp [Obj.nodes]) objects).bind fun a _ =>
        reachFrame_pure r R cells A (Val.symbol a) (by simp [SafeVal, Val.nodes])
    repeat' first
      | exact fresh
      | exact reachFrame_unknown r R cells A _ _
      | split
  | opaqueAmbientRef name | opaqueConstructorRef name => exact reachFrame_unknown r R cells A _ _
  | opaqueAmbientMemberRef receiver member => exact reachFrame_unknown r R cells A _ _
  | privateSlot op => exact reachFrame_privateSlot away op _ _ _ (safe_arg safe 0) (safe_arg safe 1) (safe_arg safe 2)
  | callWithReceiver =>
    exact hf _ _ _ (safe_arg safe 0) (safe_arg safe 1) (fun v h => safe v (List.mem_of_mem_drop h))
  | forInKeys => exact reachFrame_forInKeys away objects _ (safe_arg safe 0)
  | checkForInWrite =>
    exact reachFrame_checkForInWrite away _ _ _ (safe_arg safe 0) (safe_arg safe 1)
  | log => exact reachFrame_prim_log call args
  | create => exact reachFrame_create call args (safe_arg safe 0) objects
  | getPrototypeOf => exact reachFrame_prim_getPrototypeOf away call args safe
  | setPrototypeOf => exact reachFrame_prim_setPrototypeOf away call args safe
  | defineProperty => exact reachFrame_prim_defineProperty away call hf.getter root args safe

theorem perform_prim {r R cells A} (away : Away r R A) (call : Call W)
    (construct : Val → List Val → M W Val) (code) (hf : CallFramed r R cells A call)
    (objects : ∀ a : Addr, a.region ≠ r → A (.obj a)) (root : A (.obj objectProto))
    (p : Prim) (args : List WVal) :
    ReachFrame r R cells A (fun _ => True) (perform call construct code cells (.prim p args)) := by
  have regular (op : Prim) := (ReachFrame.mapM args fun a _ => reachFrame_fromW (W := W) r R cells A a).bind fun values safe =>
    (reachFrame_prim away call hf objects root op values safe).bind fun result hr =>
      (reachFrame_toW r R cells A result hr).bind fun v _ =>
        reachFrame_pure r R cells A (P := fun _ => True) (Told.value v) trivial
  cases p <;> first | exact regular _ | exact reachFrame_unknown r R cells A _ _

/-- All requests frame separation when the invoked functions and constructors
frame their smaller executions. End markers retain their existing refusal. -/
theorem perform_reachFrame {r R cells} (hr : r ≠ 0) (call : Call W) (construct : Val → List Val → M W Val)
    (code : Nat) (hf : CallFramed r R cells (Unprotected r R) call)
    (hc : ∀ f args, SafeVal (Unprotected r R) f → (∀ a ∈ args, SafeVal (Unprotected r R) a) →
      ReachFrame r R cells (Unprotected r R) (SafeVal (Unprotected r R)) (construct f args))
    (req : Request) :
    ReachFrame r R cells (Unprotected r R) (fun _ => True) (perform call construct code cells req) := by
  have away := unprotected_away r R
  have objects (a : Addr) (ha : a.region ≠ r) : Unprotected r R (.obj a) := ha
  have root : Unprotected r R (.obj objectProto) := Ne.symm hr
  cases req with
  | readVar i | keys i => exact perform_bindingOrKeys away call construct code _ rfl
  | writeVar i v | initVar i v => exact perform_bindingOrKeys away call construct code _ rfl
  | get o k => exact perform_get away call construct code hf.getter root o k
  | set o k v => exact perform_set away call construct code hf.setter o k v
  | delete o k => exact perform_delete away call construct code o k
  | has k o => exact perform_has away call construct code k o
  | call f this args => exact perform_call call construct code hf f this args
  | construct f args => exact perform_construct call construct code hc f args
  | newObject p => exact perform_newObject r R cells call construct code p (fun _ => Ne.symm hr)
  | newFunction part name length ctor =>
    exact perform_newFunction r R cells call construct code part name length ctor (Ne.symm hr)
  | prim p args => exact perform_prim away call construct code hf objects root p args
  | done v | throw v => exact reachFrame_unknown r R cells _ _ _
  | unknown why => exact reachFrame_unknown r R cells _ _ _

end CoreJS
