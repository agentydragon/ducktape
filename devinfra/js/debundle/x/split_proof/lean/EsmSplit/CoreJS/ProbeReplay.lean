import EsmSplit.CoreJS.HostProfile
import EsmSplit.CoreJS.LinkWorld
import EsmSplit.CoreJS.Fragment
import EsmSplit.CoreJS.Frame
import EsmSplit.CoreJS.ClassParents

/-!
# Replay of world-independent linked initialization

Replay transports a successful probe run across worlds when the reached CoreJS
operations do not depend on host answers. The executable check covers
allocation-only initializers, deferred opaque closures, own data-property
reads, and primitive logical and `void` expressions inside arrays and records.
It checks each descriptor in the reached heap; an accessor or unsupported
operation falls back to the generated proof.
-/

namespace CoreJS.LinkedBehavior

variable {w : World}

/-- A state with the CoreJS data from `s` and the initial world state of `target`. -/
def replaceWorld (target : World) {source : World} (s : State source) : State target where
  heap := s.heap
  cells := s.cells
  region := s.region
  out := s.out
  world := { state := target.init, handed := s.world.handed }

/-- Statements that are installed by linking and do no work when their unit
is reached in the evaluation schedule. Their function bodies may contain
arbitrary code because those bodies are deferred. -/
def hoistedOnly (stmt : Stmt) : Prop :=
  (∃ names, stmt = .varDecl names) ∨ (∃ f, stmt = .funDecl f)

def primitiveLiteral : Expr → Prop
  | .undef | .null | .bool _ | .num _ | .str _ => True
  | _ => False

/-- The first executable replay rules: declarations initialized by a primitive
literal. The declaration may be `var`, `let`, or `const`; link has already
allocated the corresponding cell. -/
def literalDeclaration (stmt : Stmt) : Prop :=
  ∃ kind name init, stmt = .decl kind name init ∧ primitiveLiteral init

mutual

def noClassExpr : Expr → Prop
  | .cls .. => False
  | .obj props => noClassProps props
  | .arr elems => noClassAll elems
  | .unary _ operand => noClassExpr operand
  | _ => True

def noClassAll : List Expr → Prop
  | [] => True
  | expr :: rest => noClassExpr expr ∧ noClassAll rest

def noClassProps : List PropDef → Prop
  | [] => True
  | .init _ value :: rest => noClassExpr value ∧ noClassProps rest
  | _ :: rest => noClassProps rest

end

private def NamesResolved (env : Env) : List Name → Prop
  | [] => True
  | name :: names => (env.vars.lookup name).isSome ∧ NamesResolved env names

private def namesResolvedDecidable (env : Env) : (names : List Name) → Decidable (NamesResolved env names)
  | [] => isTrue trivial
  | name :: names =>
    if found : (env.vars.lookup name).isSome then
      match namesResolvedDecidable env names with
      | isTrue rest => isTrue ⟨found, rest⟩
      | isFalse notRest => isFalse (fun h => notRest h.2)
    else isFalse (fun h => found h.1)

private instance instDecidableNamesResolved (env : Env) (names : List Name) :
    Decidable (NamesResolved env names) := namesResolvedDecidable env names

private def andDecidable (p q : Prop) (hp : Decidable p) (hq : Decidable q) : Decidable (p ∧ q) :=
  match hp, hq with
  | isTrue hp, isTrue hq => isTrue ⟨hp, hq⟩
  | isFalse hn, _ => isFalse (fun h => hn h.1)
  | _, isFalse hn => isFalse (fun h => hn h.2)

mutual

private def noClassExprDecidable : (expr : Expr) → Decidable (noClassExpr expr)
  | .cls .. => isFalse (by simp [noClassExpr])
  | .obj props =>
    match noClassPropsDecidable props with
    | isTrue h => isTrue (by simpa [noClassExpr] using h)
    | isFalse h => isFalse (by simpa [noClassExpr] using h)
  | .arr exprs =>
    match noClassAllDecidable exprs with
    | isTrue h => isTrue (by simpa [noClassExpr] using h)
    | isFalse h => isFalse (by simpa [noClassExpr] using h)
  | .unary _ expr =>
    match noClassExprDecidable expr with
    | isTrue h => isTrue (by simpa [noClassExpr] using h)
    | isFalse h => isFalse (by simpa [noClassExpr] using h)
  | .undef | .null | .bool _ | .num _ | .str _ | .regexp _ | .var _ | .host _ | .this | .func _
  | .spreadArg _ | .derived .. | .get .. | .getSuper _ | .getOptional .. | .hasProp .. | .delete ..
  | .assign .. | .assignOp .. | .assignLogical .. | .update .. | .call .. | .callMethod ..
  | .callSuper .. | .callOptionalReceiver .. | .callOptionalCallee .. | .new .. | .prim ..
  | .binary .. | .logical .. | .cond .. | .seq .. => isTrue trivial

private def noClassAllDecidable : (exprs : List Expr) → Decidable (noClassAll exprs)
  | [] => isTrue trivial
  | expr :: exprs => andDecidable (noClassExpr expr) (noClassAll exprs)
      (noClassExprDecidable expr) (noClassAllDecidable exprs)

private def noClassPropsDecidable : (props : List PropDef) → Decidable (noClassProps props)
  | [] => isTrue trivial
  | .init _ expr :: props => andDecidable (noClassExpr expr) (noClassProps props)
      (noClassExprDecidable expr) (noClassPropsDecidable props)
  | .method _ _ :: props | .getter _ _ :: props | .setter _ _ :: props
  | .proto _ :: props | .spread _ _ :: props =>
    match noClassPropsDecidable props with
    | isTrue h => isTrue (by simpa [noClassProps] using h)
    | isFalse h => isFalse (by simpa [noClassProps] using h)

end

private instance instDecidableNoClassExpr (expr : Expr) : Decidable (noClassExpr expr) :=
  noClassExprDecidable expr

def replayableExpr (env : Env) (expr : Expr) : Prop :=
  expr.allocOnly = true ∧ noClassExpr expr ∧ NamesResolved env expr.readsNow

private def replayableExprDecidable (env : Env) (expr : Expr) : Decidable (replayableExpr env expr) :=
  andDecidable (expr.allocOnly = true) (noClassExpr expr ∧ NamesResolved env expr.readsNow)
    inferInstance
    (andDecidable (noClassExpr expr) (NamesResolved env expr.readsNow)
      (noClassExprDecidable expr) (namesResolvedDecidable env expr.readsNow))

private instance instDecidableReplayableExpr (env : Env) (expr : Expr) :
    Decidable (replayableExpr env expr) := replayableExprDecidable env expr

def replayableStmt (env : Env) (stmt : Stmt) : Prop :=
  hoistedOnly stmt ∨
    (∃ kind name expr, stmt = .decl kind name expr ∧ replayableExpr env expr) ∨
    (∃ expr, stmt = .expr expr ∧ replayableExpr env expr) ∨
    (∃ code decls funs free fns, stmt = .opaque code decls funs free (.closures fns))

def ReplayableSchedule (units : List LinkedUnit) : Prop :=
  ∀ unit, unit ∈ units → replayableStmt unit.env unit.stmt

/-- Produce the finite proof object used by the compiled replay strategy. The
shape checks follow the unit list; expression eligibility is decided by its
finite syntax and binding-read footprint. -/
def replayableStmtWitness (env : Env) : (stmt : Stmt) → Option (PLift (replayableStmt env stmt))
  | .varDecl names => some ⟨Or.inl (Or.inl ⟨names, rfl⟩)⟩
  | .funDecl f => some ⟨Or.inl (Or.inr ⟨f, rfl⟩)⟩
  | .decl kind name expr =>
    if supported : replayableExpr env expr then
      some ⟨Or.inr (Or.inl ⟨kind, name, expr, rfl, supported⟩)⟩
    else none
  | .expr expr =>
    if supported : replayableExpr env expr then
      some ⟨Or.inr (Or.inr (Or.inl ⟨expr, rfl, supported⟩))⟩
    else none
  | .opaque code decls funs free (.closures fns) =>
    some ⟨Or.inr (Or.inr (Or.inr ⟨code, decls, funs, free, fns, rfl⟩))⟩
  | _ => none

/-- Turn the executable per-statement checks into the universal schedule
property consumed by the soundness theorem. -/
def replayableScheduleWitness : (units : List LinkedUnit) → Option (PLift (ReplayableSchedule units))
  | [] => some ⟨by intro unit member; simp at member⟩
  | (id, stmt, env) :: rest => do
    let statement ← replayableStmtWitness env stmt
    let tail ← replayableScheduleWitness rest
    return ⟨by
      intro unit member
      simp only [List.mem_cons] at member
      rcases member with same | member
      · cases same
        exact statement.down
      · exact tail.down unit member⟩

/-! These syntax categories report gaps in replay. The selector also uses the
typed accessor and read categories to retain the existing proof fallback.
Certificates still require an executable replay or slice proof. -/

inductive ReplayBlocker where
  | propertyRead
  | objectAccessor
  | other (label : String)
  deriving BEq

def ReplayBlocker.label : ReplayBlocker → String
  | .propertyRead => "property-read"
  | .objectAccessor => "object-accessor"
  | .other label => label

mutual

private def replayExprBlockers : Expr → List ReplayBlocker
  | .undef | .null | .bool _ | .num _ | .str _ | .regexp _ | .var _ | .host _ | .this | .func _ => []
  | .obj props => replayPropBlockers props
  | .arr elems => replayExprListBlockers elems
  | .spreadArg expr => .other "spread" :: replayExprBlockers expr
  | .cls _ _ _ members => .other "class" :: replayClassMemberBlockers members
  | .derived _ _ parent members ctor =>
    .other "derived-class" :: replayExprBlockers parent ++ replayClassMemberBlockers members ++
      (match ctor with
      | none => []
      | some (prelude, _, f) => replayStmtListBlockers prelude ++ replayFuncBlockers f)
  | .get object key => .propertyRead :: replayExprBlockers object ++ replayExprBlockers key
  | .getSuper key => .other "super-property-read" :: replayExprBlockers key
  | .getOptional object key => .other "optional-property-read" :: replayExprBlockers object ++ replayExprBlockers key
  | .hasProp key object => .other "property-test" :: replayExprBlockers key ++ replayExprBlockers object
  | .delete object key => .other "property-delete" :: replayExprBlockers object ++ replayExprBlockers key
  | .assign target rhs => .other "assignment" :: replayTargetBlockers target ++ replayExprBlockers rhs
  | .assignOp target _ rhs => .other "compound-assignment" :: replayTargetBlockers target ++ replayExprBlockers rhs
  | .assignLogical target _ rhs => .other "logical-assignment" :: replayTargetBlockers target ++ replayExprBlockers rhs
  | .update target _ _ => .other "update" :: replayTargetBlockers target
  | .call (.func f) args => .other "direct-call" :: replayFuncBlockers f ++ replayExprListBlockers args
  | .call fn args => .other "call" :: replayExprBlockers fn ++ replayExprListBlockers args
  | .callMethod object key args => .other "method-call" :: replayExprBlockers object ++ replayExprBlockers key ++ replayExprListBlockers args
  | .callSuper key args _ => .other "super-call" :: replayExprBlockers key ++ replayExprListBlockers args
  | .callOptionalReceiver object key args =>
    .other "optional-method-call" :: replayExprBlockers object ++ replayExprBlockers key ++ replayExprListBlockers args
  | .callOptionalCallee object key args property =>
    .other "optional-call" :: replayExprBlockers object ++ replayExprBlockers key ++ replayExprListBlockers args ++
      (match property with
      | none => []
      | some expr => replayExprBlockers expr)
  | .new fn args => .other "construct" :: replayExprBlockers fn ++ replayExprListBlockers args
  | .prim (.globalTypeof _) args => .other "global-typeof" :: replayExprListBlockers args
  | .prim (.globalRef _) args | .prim (.opaqueAmbientRef _) args
  | .prim (.opaqueConstructorRef _) args | .prim (.opaqueAmbientMemberRef ..) args =>
    .other "host-reference" :: replayExprListBlockers args
  | .prim _ args => .other "primitive-operation" :: replayExprListBlockers args
  | .unary _ expr => .other "unary-operation" :: replayExprBlockers expr
  | .binary _ left right => .other "binary-operation" :: replayExprBlockers left ++ replayExprBlockers right
  | .logical _ left right => .other "logical-expression" :: replayExprBlockers left ++ replayExprBlockers right
  | .cond condition yes no =>
    .other "conditional-expression" :: replayExprBlockers condition ++ replayExprBlockers yes ++ replayExprBlockers no
  | .seq left right => .other "sequence-expression" :: replayExprBlockers left ++ replayExprBlockers right

private def replayExprListBlockers : List Expr → List ReplayBlocker
  | [] => []
  | expr :: rest => replayExprBlockers expr ++ replayExprListBlockers rest

private def replayTargetBlockers : Target → List ReplayBlocker
  | .var _ => [.other "binding-write"]
  | .frozen _ => [.other "import-write"]
  | .member object key => .other "property-write" :: replayExprBlockers object ++ replayExprBlockers key

private def replayPropBlockers : List PropDef → List ReplayBlocker
  | [] => []
  | .init key value :: rest =>
    (if key matches .str _ then [] else [.other "computed-property-key"]) ++
      replayExprBlockers key ++ replayExprBlockers value ++ replayPropBlockers rest
  | .method key _ :: rest =>
    (if key matches .str _ then [] else [.other "computed-property-key"]) ++
      replayExprBlockers key ++ replayPropBlockers rest
  | .getter key _ :: rest | .setter key _ :: rest =>
    .objectAccessor :: (if key matches .str _ then [] else [.other "computed-property-key"]) ++
      replayExprBlockers key ++ replayPropBlockers rest
  | .proto value :: rest => .other "prototype-property" :: replayExprBlockers value ++ replayPropBlockers rest
  | .spread source _ :: rest => .other "object-spread" :: replayExprBlockers source ++ replayPropBlockers rest

private def replayClassMemberBlockers : List ClassMember → List ReplayBlocker
  | [] => []
  | .method _ key _ :: rest =>
    (if key matches .str _ then [] else [.other "computed-class-key"]) ++
      replayExprBlockers key ++ replayClassMemberBlockers rest
  | .getter _ key _ :: rest | .setter _ key _ :: rest =>
    .other "class-accessor" :: (if key matches .str _ then [] else [.other "computed-class-key"]) ++
      replayExprBlockers key ++ replayClassMemberBlockers rest
  | .field static key init :: rest =>
    (if static then [.other "class-static-field"] else [.other "class-field"]) ++
      (if key matches .str _ then [] else [.other "computed-class-key"]) ++
      replayExprBlockers key ++ replayExprBlockers init ++ replayClassMemberBlockers rest
  | .staticBlock body :: rest =>
    .other "class-static-block" :: replayFuncBlockers body ++ replayClassMemberBlockers rest

private def replayBindingPatternBlockers : BindingPattern → List ReplayBlocker
  | .name _ => []
  | .object properties => .other "object-binding-pattern" :: replayBindingPropertiesBlockers properties
  | .array elements rest =>
    .other "array-binding-pattern" :: replayBindingElementsBlockers elements ++
      (match rest with
      | none => []
      | some pattern => replayBindingPatternBlockers pattern)
  | .defaultValue pattern fallback =>
    .other "default-binding" :: replayBindingPatternBlockers pattern ++ replayExprBlockers fallback

private def replayBindingPropertiesBlockers : List (Expr × BindingPattern) → List ReplayBlocker
  | [] => []
  | (key, pattern) :: rest =>
    replayExprBlockers key ++ replayBindingPatternBlockers pattern ++ replayBindingPropertiesBlockers rest

private def replayBindingElementsBlockers : List (Option BindingPattern) → List ReplayBlocker
  | [] => []
  | none :: rest => replayBindingElementsBlockers rest
  | some pattern :: rest => replayBindingPatternBlockers pattern ++ replayBindingElementsBlockers rest

private def replayFuncBlockers : Func → List ReplayBlocker
  | .mk _ _ _ _ body | .withDefaults _ _ _ _ _ body | .withRest _ _ _ _ _ body =>
    replayStmtListBlockers body
  | .opaque .. => [.other "opaque-closure-call"]

private def replayStmtBlockers : Stmt → List ReplayBlocker
  | .expr expr => replayExprBlockers expr
  | .decl _ _ init => replayExprBlockers init
  | .bind _ pattern init =>
    .other "binding-pattern" :: replayBindingPatternBlockers pattern ++ replayExprBlockers init
  | .varDecl _ | .funDecl _ | .brk | .cont => []
  | .ret value => .other "return" :: replayExprBlockers value
  | .ite condition yes no =>
    .other "branch-statement" :: replayExprBlockers condition ++ replayStmtBlockers yes ++ replayStmtBlockers no
  | .block statements => .other "block" :: replayStmtListBlockers statements
  | .loop init test update body =>
    .other "loop" :: replayStmtListBlockers init ++ replayExprBlockers test ++
      replayExprBlockers update ++ replayStmtBlockers body
  | .breakScope body => .other "break-scope" :: replayStmtBlockers body
  | .forOf _ pattern source body =>
    .other "for-of-loop" :: replayBindingPatternBlockers pattern ++
      replayExprBlockers source ++ replayStmtBlockers body
  | .throw value => .other "throw" :: replayExprBlockers value
  | .tryCatch body _ handler => .other "try-catch" :: replayStmtBlockers body ++ replayStmtBlockers handler
  | .tryFinally body finalizer => .other "try-finally" :: replayStmtBlockers body ++ replayStmtBlockers finalizer
  | .opaque _ _ _ _ .world => [.other "opaque-world-code"]
  | .opaque _ _ _ _ (.closures _) => []

private def replayStmtListBlockers : List Stmt → List ReplayBlocker
  | [] => []
  | stmt :: rest => replayStmtBlockers stmt ++ replayStmtListBlockers rest

end

/-- Syntax categories present in units rejected by `ReplayableSchedule`.
The categories are not a proof witness. -/
def replayScheduleBlockers (units : List LinkedUnit) : List ReplayBlocker :=
  units.flatMap fun (_, stmt, env) =>
    if (replayableStmtWitness env stmt).isSome then []
    else
      let kinds := replayStmtBlockers stmt
      if kinds.isEmpty then [.other "unclassified-or-unresolved"] else kinds

def HoistedSchedule (units : List LinkedUnit) : Prop :=
  ∀ unit, unit ∈ units → hoistedOnly unit.stmt

private theorem runUnit_hoisted (fuel : Nat) (env : Env) (id : Nat) (stmt : Stmt)
    (hoisted : hoistedOnly stmt) (s : State w) :
    runUnit (W := w) fuel env id stmt s =
      if fuel = 0 then .fuel else .ok .normal { s with region := 0 } := by
  rcases hoisted with ⟨names, rfl⟩ | ⟨f, rfl⟩ <;>
    cases fuel <;>
      (simp [runUnit, inRegion, execStmt, modifyState, outOfFuel, pure, M.bind_apply] <;> rfl)

private def finishHoistedState (units : List LinkedUnit) (s : State w) : State w :=
  if units.isEmpty then s else { s with region := 0 }

private theorem replaceWorld_finishHoisted (target : World) (units : List LinkedUnit)
    (s : State w) :
    replaceWorld target (finishHoistedState units s) =
      finishHoistedState units (replaceWorld target s) := by
  cases units <;> rfl

private theorem runUnits_hoisted (fuel : Nat) (units : List LinkedUnit)
    (hoisted : HoistedSchedule units) (s : State w) :
    runUnits (W := w) fuel units s =
      if fuel = 0 ∧ units ≠ [] then .fuel else .ok () (finishHoistedState units s) := by
  induction units generalizing s with
  | nil => simp [runUnits, finishHoistedState, pure] <;> rfl
  | cons unit rest ih =>
    have unitHoisted := hoisted unit (by simp)
    have restHoisted : HoistedSchedule rest := by
      intro next member
      exact hoisted next (by simp [member])
    obtain ⟨id, stmt, env⟩ := unit
    cases fuel with
    | zero =>
      rcases unitHoisted with ⟨names, rfl⟩ | ⟨f, rfl⟩ <;>
        simp [runUnits, runUnit, inRegion, execStmt, modifyState, outOfFuel, M.bind_apply]
    | succ fuel =>
      have step := runUnit_hoisted (fuel + 1) env id stmt unitHoisted s
      simp only [runUnits, M.bind_apply, step]
      simp [ih restHoisted, finishHoistedState] <;> rfl

private def transportResult {source : World} {α : Type} (target : World) : Res source α → Res target α
  | .ok a s => .ok a (replaceWorld target s)
  | .throw v s => .throw v (replaceWorld target s)
  | .unknown why s => .unknown why (replaceWorld target s)
  | .fuel => .fuel

private def ReplayEquivalent {source target : World} {α : Type}
    (m : M source α) (n : M target α) : Prop :=
  ∀ s, n (replaceWorld target s) = transportResult target (m s)

private theorem replay_pure {source target : World} {α : Type} (a : α) :
    ReplayEquivalent (pure a : M source α) (pure a : M target α) := by
  intro s
  rfl

private theorem replay_bind {source target : World} {α β : Type}
    {m : M source α} {n : M target α} {f : α → M source β} {g : α → M target β}
    (hm : ReplayEquivalent m n) (hf : ∀ a, ReplayEquivalent (f a) (g a)) :
    ReplayEquivalent (m >>= f) (n >>= g) := by
  intro s
  simp only [M.bind_apply, hm s]
  cases m s with
  | ok a next => exact hf a next
  | throw value next => rfl
  | unknown why next => rfl
  | fuel => rfl

private theorem replay_map {source target : World} {α β : Type} (f : α → β)
    {m : M source α} {n : M target α} (same : ReplayEquivalent m n) :
    ReplayEquivalent (f <$> m) (f <$> n) := by
  change ReplayEquivalent (m >>= fun value => pure (f value)) (n >>= fun value => pure (f value))
  apply replay_bind same
  intro value
  exact replay_pure (f value)

private theorem replay_modify {source target : World}
    {f : State source → State source} {g : State target → State target}
    (commutes : ∀ s, g (replaceWorld target s) = replaceWorld target (f s)) :
    ReplayEquivalent (modifyState f) (modifyState g) := by
  intro s
  simp [ReplayEquivalent, modifyState, transportResult, commutes]

private theorem replay_region {source : World} (target : World) (id : Nat) (s : State source) :
    { replaceWorld target s with region := id + 1 } =
      replaceWorld target { s with region := id + 1 } := rfl

private theorem replay_inRegion {source target : World} {α : Type}
    {m : M source α} {n : M target α} (id : Nat) (h : ReplayEquivalent m n) :
    ReplayEquivalent (inRegion id m) (inRegion id n) := by
  unfold inRegion
  apply replay_bind (replay_modify (fun s => replay_region target id s))
  intro _
  apply replay_bind h
  intro a
  apply replay_bind (replay_modify (by intro s; rfl))
  intro _
  exact replay_pure a

private theorem replay_outOfFuel {source target : World} {α : Type} :
    ReplayEquivalent (outOfFuel : M source α) (outOfFuel : M target α) := by
  intro _
  rfl

private theorem replay_setCell {source target : World} (addr : Addr) (value : Val) :
    ReplayEquivalent (setCell addr value : M source Unit) (setCell addr value : M target Unit) := by
  intro s
  simp [ReplayEquivalent, setCell, transportResult, replaceWorld]

private theorem replay_unknown {source target : World} {α : Type} (why : String) :
    ReplayEquivalent (unknown why : M source α) (unknown why : M target α) := by
  intro _
  rfl

private theorem replay_throwErr {source target : World} {α : Type} (kind : ErrorKind) (msg : String) :
    ReplayEquivalent (throwErr kind msg : M source α) (throwErr kind msg : M target α) := by
  intro _
  rfl

private theorem replay_lift {source target : World} {α : Type} (result : Except Abort α) :
    ReplayEquivalent (lift result : M source α) (lift result : M target α) := by
  cases result with
  | ok value => exact replay_pure value
  | error why => cases why with
    | error kind msg => exact replay_throwErr kind msg
    | unknown why => exact replay_unknown why

private theorem replay_alloc {source target : World} (obj : Obj) :
    ReplayEquivalent (alloc obj : M source Addr) (alloc obj : M target Addr) := by
  intro s
  simp only [alloc]
  cases h : s.heap[s.region]? with
  | none => simp [h, transportResult, replaceWorld]
  | some objects => simp [h, transportResult, replaceWorld]

private theorem replay_modifyObj {source target : World} (addr : Addr) (f : Obj → Obj) :
    ReplayEquivalent (modifyObj addr f : M source Unit) (modifyObj addr f : M target Unit) := by
  intro s
  simp [ReplayEquivalent, modifyObj, transportResult, replaceWorld]

private theorem replay_newCell {source target : World} (binding : Binding) :
    ReplayEquivalent (newCell binding : M source Addr) (newCell binding : M target Addr) := by
  intro s
  simp only [newCell]
  cases h : s.cells[s.region]? with
  | none => simp [h, transportResult, replaceWorld]
  | some cells => simp [h, transportResult, replaceWorld]

private theorem replay_getObj {source target : World} (addr : Addr) :
    ReplayEquivalent (getObj addr : M source Obj) (getObj addr : M target Obj) := by
  intro s
  unfold getObj
  have sameObj : (replaceWorld target s).objAt addr = s.objAt addr := rfl
  rw [sameObj]
  cases h : s.objAt addr <;>
    simp [h, transportResult, replaceWorld]

private theorem replay_newCells {source target : World} :
    ∀ bindings : List (Name × Binding),
      ReplayEquivalent (newCells bindings : M source (List (Name × Addr)))
        (newCells bindings : M target (List (Name × Addr)))
  | [] => replay_pure []
  | (name, binding) :: rest => by
      apply replay_bind (replay_newCell binding)
      intro addr
      apply replay_bind (replay_newCells rest)
      intro frame
      exact replay_pure ((name, addr) :: frame)

private theorem replay_declare {source target : World} (env : Env) (bindings : List (Name × Binding)) :
    ReplayEquivalent (declare env bindings : M source Env) (declare env bindings : M target Env) := by
  unfold declare
  apply replay_bind (replay_newCells bindings)
  intro frame
  exact replay_pure { env with vars := frame ++ env.vars }

private theorem replay_selfScope {source target : World} (env : Env) (self : Bool) (name : Name) :
    ReplayEquivalent (selfScope env self name : M source Env) (selfScope env self name : M target Env) := by
  unfold selfScope
  split
  · exact replay_declare env [(name, { value := none, mutable := false })]
  · exact replay_pure env

private theorem replay_readVar_bound {source target : World} (env : Env) (name : Name) (addr : Addr)
    (bound : env.vars.lookup name = some addr) :
    ReplayEquivalent (readVar env name : M source Val) (readVar env name : M target Val) := by
  intro s
  have sameCell : (replaceWorld target s).cellAt addr = s.cellAt addr := rfl
  simp only [readVar, bound, getState, M.bind_apply]
  rw [sameCell]
  cases h : s.cellAt addr with
  | none => simp [h, pure, throwErr, throwVal, M.bind_apply, transportResult, replaceWorld]
  | some binding =>
    cases binding with
    | mk value mutable =>
      cases value with
      | none => simp [pure, throwErr, throwVal, M.bind_apply, transportResult, replaceWorld]
      | some value => simp [pure, throwErr, throwVal, M.bind_apply, transportResult, replaceWorld]

private theorem replay_initVar {source target : World} (env : Env) (name : Name) (value : Val) :
    ReplayEquivalent (initVar env name value : M source Unit) (initVar env name value : M target Unit) := by
  unfold initVar
  cases h : env.vars.lookup name with
  | none => exact replay_unknown "model: a declaration outside its scope"
  | some addr => exact replay_setCell addr value

private theorem replay_writeVar {source target : World} (env : Env) (name : Name) (value : Val) :
    ReplayEquivalent (writeVar env name value : M source Unit) (writeVar env name value : M target Unit) := by
  intro s
  change writeVar env name value (replaceWorld target s) =
    transportResult target (writeVar env name value s)
  cases h : env.vars.lookup name with
  | none => simp [writeVar, h, throwErr, throwVal, transportResult]
  | some addr =>
    have mappedCell : (replaceWorld target s).cellAt addr = s.cellAt addr := rfl
    simp only [writeVar, h, getState, M.bind_apply]
    cases hc : s.cellAt addr with
    | none =>
      rw [mappedCell, hc]
      rfl
    | some binding =>
      cases binding with
      | mk current mutable =>
        cases current with
        | none =>
          rw [mappedCell, hc]
          rfl
        | some current =>
          cases mutable with
          | false =>
            rw [mappedCell, hc]
            rfl
          | true =>
            rw [mappedCell, hc]
            exact replay_setCell addr value s

private theorem replay_addPrototype {source target : World} (addr : Addr) (kind : FuncKind) :
    ReplayEquivalent (addPrototype addr kind : M source Unit) (addPrototype addr kind : M target Unit) := by
  unfold addPrototype
  apply replay_bind (replay_alloc {
    proto := some kind.instanceProto,
    props := if kind == .normal then [ ("constructor", .data (.obj addr) true false true) ] else [] })
  intro prototype
  apply replay_bind (replay_modifyObj addr fun obj => { obj with props := obj.props ++
    [(PropertyKey.str "prototype", Property.data (.obj prototype) true false false)] })
  intro _
  exact replay_pure ()

private theorem replay_initSelf {source target : World} (env : Env) (self : Bool) (name : Name) (value : Val) :
    ReplayEquivalent (initSelf env self name value : M source Unit)
      (initSelf env self name value : M target Unit) := by
  unfold initSelf
  split
  · exact replay_initVar env name value
  · exact replay_pure ()

private theorem replay_makeFunc {source target : World} (env : Env) (f : Func) (displayName : String := f.name) :
    ReplayEquivalent (makeFunc env f displayName : M source Val)
      (makeFunc env f displayName : M target Val) := by
  unfold makeFunc
  apply replay_bind (replay_selfScope env f.self f.name)
  intro scopedEnv
  let captured := scopedEnv.capture f.closes
  let captured := if f.kind == .normal then { captured with home := none } else captured
  apply replay_bind (replay_alloc {
    proto := some f.intrinsicProto, code := some (.closure f captured []),
    props := [("length", .data (.num (Float.ofNat f.length)) false false true),
      ("name", .data (.str displayName) false false true)] })
  intro addr
  have prototype : ReplayEquivalent
      (if f.kind.hasOwnPrototype then (addPrototype (W := source) addr f.kind : M source Unit)
        else (pure () : M source Unit))
      (if f.kind.hasOwnPrototype then (addPrototype (W := target) addr f.kind : M target Unit)
        else (pure () : M target Unit)) := by
    cases h : f.kind.hasOwnPrototype <;> simp [h]
    · exact replay_pure ()
    · exact replay_addPrototype addr f.kind
  apply replay_bind prototype
  intro _
  apply replay_bind (replay_initSelf scopedEnv f.self f.name (.obj addr))
  intro _
  exact replay_pure (Val.obj addr)

private theorem replay_makeClassMethod {source target : World} (env : Env) (home : Addr)
    (key : PropertyKey) (affix : String) (f : Func) :
    ReplayEquivalent (makeClassMethod env home key affix f : M source Val)
      (makeClassMethod env home key affix f : M target Val) := by
  cases key with
  | str name => exact replay_makeFunc { env with home := some home } f (affix ++ name)
  | wellKnownSymbol sym => exact replay_makeFunc { env with home := some home } f (affix ++ s!"[{sym.description}]")
  | symbol addr => exact replay_unknown "computed class method needs an unmodeled symbol description"

private theorem replay_makeOpaque {source target : World} (code part : Nat) (cells : List (Option Addr))
    (name : String) (length : Nat) (constructor : Bool) (proto : Addr := functionProto) :
    ReplayEquivalent (makeOpaque code part cells name length constructor proto : M source Val)
      (makeOpaque code part cells name length constructor proto : M target Val) := by
  unfold makeOpaque
  apply replay_bind (replay_alloc {
    proto := some proto, code := some (.opaque code part cells constructor),
    props := [("length", .data (.num (Float.ofNat length)) false false true),
      ("name", .data (.str name) false false true)] })
  intro addr
  have prototype : ReplayEquivalent
      (if constructor then (addPrototype (W := source) addr : M source Unit)
        else (pure () : M source Unit))
      (if constructor then (addPrototype (W := target) addr : M target Unit)
        else (pure () : M target Unit)) := by
    cases h : constructor with
    | false => simp [h]; exact replay_pure ()
    | true => simp [h]; exact replay_addPrototype addr .normal
  apply replay_bind prototype
  intro _
  exact replay_pure (Val.obj addr)

private theorem replay_defineOwn {source target : World} (addr : Addr) (key : PropertyKey) (desc : Desc) :
    ReplayEquivalent (defineOwn addr key desc : M source Unit) (defineOwn addr key desc : M target Unit) := by
  unfold defineOwn
  apply replay_bind (replay_getObj addr)
  intro obj
  by_cases frozen : obj.frozen && (obj.props.lookup key).isNone
  · simp [frozen]
    exact replay_throwErr .type "cannot add property to a frozen object"
  · by_cases array : obj.array && !obj.frozen && key.string?.isSome
    · simp [frozen, array]
      exact replay_unknown "a property is defined on an array"
    · simp [frozen, array]
      apply replay_bind (replay_lift (applyDesc (obj.props.lookup key) desc))
      intro property
      cases property with
      | none => exact replay_throwErr .type s!"cannot redefine property: {key}"
      | some property =>
        apply replay_bind (replay_modifyObj addr fun current =>
          { current with props := setOwn current.props key property })
        intro _
        exact replay_pure ()

private theorem replay_createData {source target : World} (addr : Addr) (key : PropertyKey) (value : Val) :
    ReplayEquivalent (createData addr key value : M source Unit)
      (createData addr key value : M target Unit) := by
  unfold createData
  exact replay_defineOwn addr key
    { value := some value, writable := some true, enumerable := some true, configurable := some true }

private theorem replay_toKey_string {source target : World} (name : String) :
    ReplayEquivalent (toKey (.str name) : M source PropertyKey)
      (toKey (.str name) : M target PropertyKey) := by
  intro _
  rfl

private theorem NamesResolved.get {env : Env} {names : List Name} {name : Name}
    (resolved : NamesResolved env names) (member : name ∈ names) : (env.vars.lookup name).isSome := by
  induction names with
  | nil => simp at member
  | cons head rest ih =>
    simp only [NamesResolved, List.mem_cons] at resolved member
    rcases member with same | member
    · subst name
      exact resolved.1
    · exact ih resolved.2 member

private theorem NamesResolved.of_subset {env : Env} {names subset : List Name}
    (resolved : NamesResolved env names) (included : ∀ name, name ∈ subset → name ∈ names) :
    NamesResolved env subset := by
  induction subset with
  | nil => trivial
  | cons head rest ih =>
    constructor
    · exact resolved.get (included head (by simp))
    · apply ih
      intro name member
      exact included name (by simp [member])

private theorem replay_literalArithmetic {source target : World} :
    ∀ (fuel : Nat) (env : Env) (expr : Expr), expr.literalArithmetic = true →
      ReplayEquivalent (evalExpr fuel env expr : M source Val) (evalExpr fuel env expr : M target Val) := by
  intro fuel
  induction fuel with
  | zero => intro env expr _; exact replay_outOfFuel
  | succ fuel ih =>
    intro env expr literal
    cases expr with
    | num value =>
      simp only [evalExpr]
      exact replay_pure (Val.num value)
    | binary op left right =>
      simp only [Expr.literalArithmetic, Bool.and_eq_true] at literal
      unfold evalExpr
      rcases literal with ⟨⟨_, leftLiteral⟩, rightLiteral⟩
      apply replay_bind (ih env left leftLiteral)
      intro leftValue
      apply replay_bind (ih env right rightLiteral)
      intro rightValue
      exact replay_lift (binop op leftValue rightValue)
    | _ => simp only [Expr.literalArithmetic, Bool.false_eq_true] at literal

private theorem replay_allocOnly {source target : World} :
    ∀ (fuel : Nat),
      (∀ env expr, expr.allocOnly = true → noClassExpr expr →
        NamesResolved env expr.readsNow →
        ReplayEquivalent (evalExpr fuel env expr : M source Val) (evalExpr fuel env expr : M target Val)) ∧
      (∀ env exprs, allocOnlyAll exprs = true → noClassAll exprs →
        NamesResolved env (readsNowAll exprs) →
        ReplayEquivalent (evalArgs fuel env exprs : M source (List Val))
          (evalArgs fuel env exprs : M target (List Val))) ∧
      (∀ env addr props, allocOnlyProps props = true → noClassProps props →
        NamesResolved env (readsNowProps props) →
        ReplayEquivalent (evalProps fuel env addr props : M source Unit)
          (evalProps fuel env addr props : M target Unit)) := by
  intro fuel
  induction fuel with
  | zero =>
    refine ⟨?_, ?_, ?_⟩
    · intro env expr _ _ _; exact replay_outOfFuel
    · intro env exprs _ _ _; exact replay_outOfFuel
    · intro env addr props _ _ _; exact replay_outOfFuel
  | succ fuel ih =>
    obtain ⟨ihExpr, ihArgs, ihProps⟩ := ih
    refine ⟨?_, ?_, ?_⟩
    · intro env expr allocOnly noClass resolved
      cases expr with
      | undef => simp only [evalExpr]; exact replay_pure Val.undef
      | null => simp only [evalExpr]; exact replay_pure Val.null
      | bool value => simp only [evalExpr]; exact replay_pure (Val.bool value)
      | num value => simp only [evalExpr]; exact replay_pure (Val.num value)
      | str value => simp only [evalExpr]; exact replay_pure (Val.str value)
      | var name =>
        simp only [evalExpr]
        have bound := resolved.get (name := name) (by simp [Expr.readsNow])
        obtain ⟨addr, haddr⟩ := Option.isSome_iff_exists.mp bound
        exact replay_readVar_bound env name addr haddr
      | host name => simp only [evalExpr]; exact replay_pure (Val.host name)
      | this => simp only [evalExpr]; exact replay_pure env.this
      | func f => simp only [evalExpr]; exact replay_makeFunc env f
      | regexp data =>
        simp only [evalExpr]
        exact replay_map Val.obj (replay_alloc (regexpObject data))
      | binary op left right =>
        have literal : (Expr.binary op left right).literalArithmetic = true := by
          simpa [Expr.allocOnly] using allocOnly
        exact replay_literalArithmetic (fuel + 1) env (.binary op left right) literal
      | unary op operand =>
        simp only [Expr.allocOnly, Bool.and_eq_true] at allocOnly
        have noClassOperand : noClassExpr operand := by simpa [noClassExpr] using noClass
        have resolvedOperand : NamesResolved env operand.readsNow := by
          simpa [Expr.readsNow] using resolved
        rw [evalExpr_localUnary (W := source) allocOnly.1 fuel env]
        rw [evalExpr_localUnary (W := target) allocOnly.1 fuel env]
        apply replay_bind (ihExpr env operand allocOnly.2 noClassOperand resolvedOperand)
        intro value
        have same : unop (initialState source 0) op value = unop (initialState target 0) op value := by
          rcases localUnaryOperand_ops allocOnly.1 with h | h <;> cases h <;> simp [unop]
        rw [same]
        exact replay_lift _
      | obj props =>
        simp only [Expr.allocOnly] at allocOnly
        have noClassProps' : noClassProps props := by simpa [noClassExpr] using noClass
        have resolvedProps : NamesResolved env (readsNowProps props) := by
          simpa [Expr.readsNow] using resolved
        simp only [evalExpr]
        apply replay_bind (replay_alloc { proto := some objectProto, props := [] })
        intro addr
        apply replay_bind (ihProps env addr props allocOnly noClassProps' resolvedProps)
        intro _
        exact replay_pure (Val.obj addr)
      | arr exprs =>
        simp only [Expr.allocOnly] at allocOnly
        have noClassExprs : noClassAll exprs := by simpa [noClassExpr] using noClass
        have resolvedExprs : NamesResolved env (readsNowAll exprs) := by
          simpa [Expr.readsNow] using resolved
        simp only [evalExpr]
        apply replay_bind (ihArgs env exprs allocOnly noClassExprs resolvedExprs)
        intro values
        apply replay_bind (replay_alloc (arrayObject values))
        intro addr
        exact replay_pure (Val.obj addr)
      | cls name self ctor members => simp [noClassExpr] at noClass
      | _ => simp only [Expr.allocOnly, Bool.false_eq_true] at allocOnly
    · intro env exprs allocOnly noClass resolved
      cases exprs with
      | nil =>
        simp only [evalArgs]
        exact replay_pure ([] : List Val)
      | cons expr rest =>
        simp only [allocOnlyAll, Bool.and_eq_true] at allocOnly
        rcases noClass with ⟨noClassExpr', noClassRest⟩
        have resolvedExpr : NamesResolved env expr.readsNow := by
          apply NamesResolved.of_subset resolved
          intro name member
          simp [readsNowAll, member]
        have resolvedRest : NamesResolved env (readsNowAll rest) := by
          apply NamesResolved.of_subset resolved
          intro name member
          simp [readsNowAll, member]
        simp only [evalArgs, Expr.argument_of_allocOnly allocOnly.1, ↓reduceIte, pure, M.bind_apply]
        apply replay_bind (ihExpr env expr allocOnly.1 noClassExpr' resolvedExpr)
        intro value
        apply replay_bind (ihArgs env rest allocOnly.2 noClassRest resolvedRest)
        intro values
        exact replay_pure (value :: values)
    · intro env addr props allocOnly noClass resolved
      cases props with
      | nil => exact replay_pure ()
      | cons prop rest =>
        cases prop with
        | init key value =>
          cases key with
          | str name =>
            cases fuel with
            | zero =>
              simp only [evalProps, evalExpr]
              exact replay_outOfFuel
            | succ fuel =>
              simp only [allocOnlyProps, Bool.and_eq_true] at allocOnly
              rcases noClass with ⟨noClassValue, noClassRest⟩
              have resolvedValue : NamesResolved env value.readsNow := by
                apply NamesResolved.of_subset resolved
                intro name member
                simp [readsNowProps, member]
              have resolvedRest : NamesResolved env (readsNowProps rest) := by
                apply NamesResolved.of_subset resolved
                intro name member
                simp [readsNowProps, member]
              simp only [evalProps, evalExpr, toKey, toStr, lift, pure, M.bind_apply]
              apply replay_bind (ihExpr env value allocOnly.1 noClassValue resolvedValue)
              intro initValue
              apply replay_bind (replay_createData addr (.str name) initValue)
              intro _
              exact ihProps env addr rest allocOnly.2 noClassRest resolvedRest
          | _ => simp only [allocOnlyProps, Bool.false_eq_true] at allocOnly
        | method key f =>
          cases key with
          | str name =>
            cases fuel with
            | zero =>
              simp only [evalProps, evalExpr]
              exact replay_outOfFuel
            | succ fuel =>
              simp only [allocOnlyProps, noClassProps] at allocOnly noClass
              simp only [evalProps, evalExpr, toKey, toStr, lift, pure, M.bind_apply]
              apply replay_bind (replay_makeClassMethod env addr (.str name) "" f)
              intro method
              apply replay_bind (replay_createData addr (.str name) method)
              intro _
              exact ihProps env addr rest allocOnly noClass (by
                simpa [readsNowProps] using resolved)
          | _ => simp only [allocOnlyProps, Bool.false_eq_true] at allocOnly
        | getter key f | setter key f | proto key | spread key _ =>
          simp only [allocOnlyProps, Bool.false_eq_true] at allocOnly

private theorem replay_evalReplayable {source target : World} (fuel : Nat) (env : Env) (expr : Expr)
    (supported : replayableExpr env expr) :
    ReplayEquivalent (evalExpr fuel env expr : M source Val) (evalExpr fuel env expr : M target Val) := by
  rcases supported with ⟨allocOnly, noClass, resolved⟩
  exact (replay_allocOnly (source := source) (target := target) fuel).1 env expr allocOnly noClass resolved

/-- A successful probe computation together with its transport to every world.
The witnesses are constructed by compiled checks on the reached probe states. -/
private structure ReplaySuccess {source : World} {α : Type}
    (m : (w : World) → M w α) (s : State source) where
  value : α
  next : State source
  run : m source s = .ok value next
  replay : ∀ target, m target (replaceWorld target s) = .ok value (replaceWorld target next)

private def replaySuccessOfPure {source : World} {α : Type} (value : α) (s : State source) :
    ReplaySuccess (fun w => (pure value : M w α)) s :=
  ⟨value, s, rfl, by intro target; rfl⟩

private def replaySuccessBind {source : World} {α β : Type}
    {m : (w : World) → M w α} {f : (w : World) → α → M w β} {s : State source}
    (first : ReplaySuccess m s) (second : ReplaySuccess (fun w => f w first.value) first.next) :
    ReplaySuccess (fun w => m w >>= f w) s where
  value := second.value
  next := second.next
  run := by simp only [M.bind_apply, first.run, second.run]
  replay := by
    intro target
    simp only [M.bind_apply, first.replay target, second.replay target]

private def replaySuccessOfEquivalent {source : World} {α : Type}
    (m : (w : World) → M w α) (s : State source)
    (same : ∀ target, ReplayEquivalent (m source) (m target)) : Option (ReplaySuccess m s) :=
  match h : m source s with
  | .ok value next => some {
      value := value
      next := next
      run := h
      replay := by
        intro target
        rw [same target s, h]
        rfl }
  | .throw .. | .unknown .. | .fuel => none

/-- `void` evaluates its operand before discarding its value. -/
theorem evalExpr_void (fuel : Nat) (env : Env) (operand : Expr) :
    (evalExpr (W := W) (fuel + 1) env (.unary .void operand)) =
      (evalExpr fuel env operand >>= fun _ => pure Val.undef) := by
  simp only [evalExpr]
  apply congrArg (fun f : Val → M W Val => evalExpr fuel env operand >>= f)
  funext value state
  rfl

/-- Check the actual own descriptor before bypassing the accessor branch.
The ordinary evaluator still performs the read in both worlds. -/
private def replaySuccessOwnData {source : World} (fuel : Nat) (base key : Val) (s : State source) :
    Option (ReplaySuccess (fun w => getProp (callFn (W := w) fuel) base key) s) :=
  match base, key with
  | .obj addr, .str name =>
    match object : s.objAt addr with
    | none => none
    | some obj =>
      match property : obj.props.lookup (.str name) with
      | some (.data value writable enumerable configurable) => some {
          value := value
          next := s
          run := getProp_ownData (callFn fuel) object property
          replay := by
            intro target
            exact getProp_ownData (callFn fuel)
              (show (replaceWorld target s).objAt addr = some obj from object) property }
      | _ => none
  | _, _ => none

/-- Keep function and host identities on the existing fallback path. -/
private def primitiveReplayValue : Val → Bool
  | .undef | .null | .bool _ | .num _ | .str _ => true
  | _ => false

/- A literal can make the entire right operand unreachable. Use the same
short-circuit operation as the evaluator, rather than classifying the syntax
in that operand: it creates no functions or other values when it is skipped.
The replay witness below still checks the actual decision in each schedule. -/
private def literalShortCircuits (op : LogOp) (expr : Expr) : Bool :=
  let value : Option Val := match expr with
    | .undef => some .undef
    | .null => some .null
    | .bool b => some (.bool b)
    | .num n => some (.num n)
    | .str s => some (.str s)
    | _ => none
  match value with
  | some v => match shortCircuits op v with
    | .ok true => true
    | _ => false
  | none => false

/- Reached syntax remains restricted to primitives, binding reads and plain
arrays and records, keeping functions, host values and accessors on the
fallback path. -/
mutual

private def primitiveLogicalExpr : Expr → Bool
  | .undef | .null | .bool _ | .num _ | .str _ | .var _ => true
  | .unary .void operand => primitiveLogicalExpr operand
  | .logical op left right =>
      primitiveLogicalExpr left && (literalShortCircuits op left || primitiveLogicalExpr right)
  | .arr values => primitiveLogicalExprs values
  | .obj props => primitiveLogicalProps props
  | _ => false

private def primitiveLogicalExprs : List Expr → Bool
  | [] => true
  | value :: rest => primitiveLogicalExpr value && primitiveLogicalExprs rest

private def primitiveLogicalProps : List PropDef → Bool
  | [] => true
  | .init (.str _) value :: rest => primitiveLogicalExpr value && primitiveLogicalProps rest
  | _ => false

end

private def primitiveLogicalStmt : Stmt → Bool
  | .decl _ _ expr | .expr expr => primitiveLogicalExpr expr
  | .varDecl _ => true
  | _ => false

mutual

private def checkReplayExpr {source : World} (allowLogical : Bool) : (fuel : Nat) → (env : Env) → (expr : Expr) →
    (s : State source) → Option (ReplaySuccess (fun w => evalExpr (W := w) fuel env expr) s)
  | 0, _, _, _ => none
  | n + 1, env, expr, s =>
    if supported : replayableExpr env expr then
      replaySuccessOfEquivalent (fun w => evalExpr (W := w) (n + 1) env expr) s
        (fun _ => replay_evalReplayable (n + 1) env expr supported)
    else
      match expr with
      | .unary .void operand => do
        guard allowLogical
        let evaluated ← checkReplayExpr allowLogical n env operand s
        let result := replaySuccessBind
          (f := fun w _ => (pure Val.undef : M w Val)) evaluated
          (replaySuccessOfPure Val.undef evaluated.next)
        have result' : ReplaySuccess (fun w => evalExpr (W := w) (n + 1) env (.unary .void operand)) s := by
          simpa only [evalExpr_void] using result
        pure result'
      | .logical op left right => do
        guard allowLogical
        let leftRun ← checkReplayExpr allowLogical n env left s
        guard (primitiveReplayValue leftRun.value)
        let decision ← replaySuccessOfEquivalent
          (fun w => lift (W := w) (shortCircuits op leftRun.value)) leftRun.next
          (fun _ => replay_lift (shortCircuits op leftRun.value))
        let selected : ReplaySuccess
            (fun w => if decision.value then (pure leftRun.value : M w Val) else evalExpr n env right)
            decision.next ←
          if stop : decision.value then
            some (by simpa only [stop, ↓reduceIte] using replaySuccessOfPure leftRun.value decision.next)
          else do
            let rightRun ← checkReplayExpr allowLogical n env right decision.next
            guard (primitiveReplayValue rightRun.value)
            pure (by simpa only [stop, Bool.false_eq_true, ↓reduceIte] using rightRun)
        let decisionStep := replaySuccessBind (f := fun w stop =>
          if stop then (pure leftRun.value : M w Val) else evalExpr n env right) decision selected
        let result := replaySuccessBind (f := fun w value => do
          if ← lift (W := w) (shortCircuits op value) then pure value else evalExpr n env right)
          leftRun decisionStep
        have result' : ReplaySuccess (fun w => evalExpr (W := w) (n + 1) env (.logical op left right)) s := by
          simpa only [evalExpr] using result
        pure result'
      | .get object key => do
        let objectRun ← checkReplayExpr allowLogical n env object s
        let keyRun ← checkReplayExpr allowLogical n env key objectRun.next
        let readRun ← replaySuccessOwnData n objectRun.value keyRun.value keyRun.next
        let keyStep := replaySuccessBind
          (f := fun w keyValue => getProp (callFn (W := w) n) objectRun.value keyValue) keyRun readRun
        let result := replaySuccessBind (f := fun w base => do
          let keyValue ← evalExpr (W := w) n env key
          getProp (callFn n) base keyValue) objectRun keyStep
        have result' : ReplaySuccess (fun w => evalExpr (W := w) (n + 1) env (.get object key)) s := by
          simpa only [evalExpr] using result
        pure result'
      | .arr exprs => do
        let elements ← checkReplayArgs allowLogical n env exprs s
        let allocated ← replaySuccessOfEquivalent (fun w => alloc (W := w) (arrayObject elements.value))
          elements.next (fun _ => replay_alloc (arrayObject elements.value))
        let allocation := replaySuccessBind (f := fun w addr => (pure (Val.obj addr) : M w Val))
          allocated (replaySuccessOfPure (Val.obj allocated.value) allocated.next)
        let result := replaySuccessBind (f := fun w values => do
          let addr ← alloc (W := w) (arrayObject values)
          pure (Val.obj addr)) elements allocation
        have result' : ReplaySuccess (fun w => evalExpr (W := w) (n + 1) env (.arr exprs)) s := by
          simpa only [evalExpr] using result
        pure result'
      | .obj props => do
        let allocated ← replaySuccessOfEquivalent
          (fun w => alloc (W := w) { proto := some objectProto, props := [] }) s
          (fun _ => replay_alloc { proto := some objectProto, props := [] })
        let fields ← checkReplayProps allowLogical n env allocated.value props allocated.next
        let fieldStep := replaySuccessBind (f := fun w _ => (pure (Val.obj allocated.value) : M w Val))
          fields (replaySuccessOfPure (Val.obj allocated.value) fields.next)
        let result := replaySuccessBind (f := fun w addr => do
          evalProps (W := w) n env addr props
          pure (Val.obj addr)) allocated fieldStep
        have result' : ReplaySuccess (fun w => evalExpr (W := w) (n + 1) env (.obj props)) s := by
          simpa only [evalExpr] using result
        pure result'
      | _ => none

private def checkReplayArgs {source : World} (allowLogical : Bool) : (fuel : Nat) → (env : Env) → (exprs : List Expr) →
    (s : State source) → Option (ReplaySuccess (fun w => evalArgs (W := w) fuel env exprs) s)
  | 0, _, _, _ => none
  | _ + 1, _, [], s => some (replaySuccessOfPure [] s)
  | n + 1, env, expr :: rest, s =>
    match argument : expr.argument with
    | (true, _) => none
    | (false, operand) => do
      let head ← checkReplayExpr allowLogical n env operand s
      let tail ← checkReplayArgs allowLogical n env rest head.next
      let tailStep := replaySuccessBind
        (f := fun w values => (pure ([head.value] ++ values) : M w (List Val))) tail
        (replaySuccessOfPure ([head.value] ++ tail.value) tail.next)
      let result := replaySuccessBind (f := fun w value => do
        let values ← (pure [value] : M w (List Val))
        return values ++ (← evalArgs n env rest)) head tailStep
      have result' : ReplaySuccess (fun w => evalArgs (W := w) (n + 1) env (expr :: rest)) s := by
        simpa only [evalArgs, argument, Bool.false_eq_true, ↓reduceIte, M.pure_bind] using result
      pure result'

private def checkReplayProps {source : World} (allowLogical : Bool) : (fuel : Nat) → (env : Env) → (addr : Addr) →
    (props : List PropDef) → (s : State source) →
    Option (ReplaySuccess (fun w => evalProps (W := w) fuel env addr props) s)
  | 0, _, _, _, _ => none
  | _ + 1, _, _, [], s => some (replaySuccessOfPure () s)
  | 1, _, _, _ :: _, _ => none
  | n + 2, env, addr, .init (.str name) value :: rest, s => do
    let init ← checkReplayExpr allowLogical (n + 1) env value s
    let created ← replaySuccessOfEquivalent
      (fun w => createData (W := w) addr (.str name) init.value) init.next
      (fun _ => replay_createData addr (.str name) init.value)
    let tail ← checkReplayProps allowLogical (n + 1) env addr rest created.next
    let createStep := replaySuccessBind (f := fun w _ => evalProps (W := w) (n + 1) env addr rest)
      created tail
    let result := replaySuccessBind (f := fun w initValue => do
      createData (W := w) addr (.str name) initValue
      evalProps (n + 1) env addr rest) init createStep
    have result' : ReplaySuccess
        (fun w => evalProps (W := w) (n + 2) env addr (.init (.str name) value :: rest)) s := by
      have keyEq (w : World) : (toKey (.str name) : M w PropertyKey) = pure (.str name) := by
        funext state
        rfl
      simpa only [evalProps, evalExpr, keyEq, M.pure_bind] using result
    pure result'
  | _ + 2, _, _, _ :: _, _ => none

end

private theorem replay_initClosures {source target : World} (code : Nat) (cells : List (Option Addr))
    (env : Env) :
    ∀ (decls : List (Name × DeclKind)) (fns : List (String × Nat × Bool)) (part : Nat),
      ReplayEquivalent (initClosures code cells env decls fns part : M source Unit)
        (initClosures code cells env decls fns part : M target Unit)
  | [], [], _ => replay_pure ()
  | (name, kind) :: decls, (displayName, length, constructor) :: fns, part => by
      unfold initClosures
      apply replay_bind (replay_makeOpaque code part cells displayName length constructor)
      intro value
      cases kind with
      | var =>
        apply replay_bind (replay_writeVar env name value)
        intro _
        exact replay_initClosures code cells env decls fns (part + 1)
      | let_ | const =>
        apply replay_bind (replay_initVar env name value)
        intro _
        exact replay_initClosures code cells env decls fns (part + 1)
  | [], (displayName, length, constructor) :: fns, part => by
      change ReplayEquivalent
        (unknown "model: an opaque statement has as many functions as declarations" : M source Unit)
        (unknown "model: an opaque statement has as many functions as declarations" : M target Unit)
      exact replay_unknown _
  | (name, kind) :: decls, [], part => by
      change ReplayEquivalent
        (unknown "model: an opaque statement has as many functions as declarations" : M source Unit)
        (unknown "model: an opaque statement has as many functions as declarations" : M target Unit)
      exact replay_unknown _

private theorem replay_execReplayable {source target : World} (fuel : Nat) (env : Env) (stmt : Stmt)
    (supported : replayableStmt env stmt) :
    ReplayEquivalent (execStmt fuel env stmt : M source Flow) (execStmt fuel env stmt : M target Flow) := by
  rcases supported with hoisted | declaration | expression | opaqueClosures
  · rcases hoisted with ⟨names, rfl⟩ | ⟨f, rfl⟩
    · cases fuel with
      | zero => exact replay_outOfFuel
      | succ fuel => exact replay_pure Flow.normal
    · cases fuel with
      | zero => exact replay_outOfFuel
      | succ fuel => exact replay_pure Flow.normal
  · rcases declaration with ⟨kind, name, expr, rfl, supportedExpr⟩
    cases fuel with
    | zero => exact replay_outOfFuel
    | succ fuel =>
      cases kind with
      | var =>
        unfold execStmt
        apply replay_bind (replay_evalReplayable fuel env expr supportedExpr)
        intro value
        apply replay_bind (replay_writeVar env name value)
        intro _
        exact replay_pure Flow.normal
      | let_ | const =>
        unfold execStmt
        apply replay_bind (replay_evalReplayable fuel env expr supportedExpr)
        intro value
        apply replay_bind (replay_initVar env name value)
        intro _
        exact replay_pure Flow.normal
  · rcases expression with ⟨expr, rfl, supportedExpr⟩
    cases fuel with
    | zero => exact replay_outOfFuel
    | succ fuel =>
      unfold execStmt
      apply replay_bind (replay_evalReplayable fuel env expr supportedExpr)
      intro _
      exact replay_pure Flow.normal
  · rcases opaqueClosures with ⟨code, decls, funs, free, fns, rfl⟩
    cases fuel with
    | zero => exact replay_outOfFuel
    | succ fuel =>
      unfold execStmt
      apply replay_bind (replay_initClosures code (opaqueCells env decls funs free) env decls fns
        (funs.length + 1))
      intro _
      exact replay_pure Flow.normal

private def checkReplayStmtExtra {source : World} (allowLogical : Bool) : (fuel : Nat) → (env : Env) → (stmt : Stmt) →
    (s : State source) → Option (ReplaySuccess (fun w => execStmt (W := w) fuel env stmt) s)
  | n + 1, env, .expr expr, s => do
    let evaluated ← checkReplayExpr allowLogical n env expr s
    let result := replaySuccessBind (f := fun w _ => (pure Flow.normal : M w Flow))
      evaluated (replaySuccessOfPure Flow.normal evaluated.next)
    have result' : ReplaySuccess (fun w => execStmt (W := w) (n + 1) env (.expr expr)) s := by
      simpa only [execStmt] using result
    pure result'
  | n + 1, env, .decl kind name expr, s => do
    let evaluated ← checkReplayExpr allowLogical n env expr s
    let written ← replaySuccessOfEquivalent
      (fun w => if kind == .var then writeVar (W := w) env name evaluated.value
        else initVar (W := w) env name evaluated.value) evaluated.next
      (fun _ => by
        cases kind with
        | var => exact replay_writeVar env name evaluated.value
        | let_ | const => exact replay_initVar env name evaluated.value)
    let writeStep := replaySuccessBind (f := fun w _ => (pure Flow.normal : M w Flow))
      written (replaySuccessOfPure Flow.normal written.next)
    let result := replaySuccessBind (f := fun w value =>
      (if kind == .var then writeVar (W := w) env name value else initVar env name value) >>=
        fun _ => pure Flow.normal) evaluated writeStep
    have result' : ReplaySuccess (fun w => execStmt (W := w) (n + 1) env (.decl kind name expr)) s := by
      cases kind <;> simpa [execStmt] using result
    pure result'
  | _, _, _, _ => none

private def checkReplayStmt {source : World} (allowLogical : Bool) (fuel : Nat) (env : Env) (stmt : Stmt) (s : State source) :
    Option (ReplaySuccess (fun w => execStmt (W := w) fuel env stmt) s) :=
  if let some supported := replayableStmtWitness env stmt then
    replaySuccessOfEquivalent (fun w => execStmt (W := w) fuel env stmt) s
      (fun _ => replay_execReplayable fuel env stmt supported.down)
  else checkReplayStmtExtra allowLogical fuel env stmt s

private def checkReplayUnit {source : World} (allowLogical : Bool) (fuel : Nat) (env : Env) (id : Nat) (stmt : Stmt)
    (s : State source) : Option (ReplaySuccess (fun w => runUnit (W := w) fuel env id stmt) s) := do
  let entered ← replaySuccessOfEquivalent
    (fun w => modifyState (W := w) fun state => { state with region := id + 1 }) s
    (fun _ => replay_modify (by intro state; rfl))
  let executed ← checkReplayStmt allowLogical fuel env stmt entered.next
  let exited ← replaySuccessOfEquivalent
    (fun w => modifyState (W := w) fun state => { state with region := 0 }) executed.next
    (fun _ => replay_modify (by intro state; rfl))
  let exitStep := replaySuccessBind (f := fun w _ => (pure executed.value : M w Flow))
    exited (replaySuccessOfPure executed.value exited.next)
  let execution := replaySuccessBind (f := fun w flow => do
    modifyState (W := w) fun state => { state with region := 0 }
    pure flow) executed exitStep
  let result := replaySuccessBind (f := fun w _ => do
    let flow ← execStmt (W := w) fuel env stmt
    modifyState fun state => { state with region := 0 }
    pure flow) entered execution
  have result' : ReplaySuccess (fun w => runUnit (W := w) fuel env id stmt) s := by
    simpa only [runUnit, inRegion] using result
  pure result'

private def checkReplayUnits {source : World} (allowLogical : Bool) (fuel : Nat) :
    (units : List LinkedUnit) → (s : State source) →
      Option (ReplaySuccess (fun w => runUnits (W := w) fuel units) s)
  | [], s => some (replaySuccessOfPure () s)
  | (id, stmt, env) :: rest, s => do
    let head ← checkReplayUnit allowLogical fuel env id stmt s
    let tail ← checkReplayUnits allowLogical fuel rest head.next
    let result := replaySuccessBind (f := fun w _ => runUnits (W := w) fuel rest) head tail
    have result' : ReplaySuccess (fun w => runUnits (W := w) fuel ((id, stmt, env) :: rest)) s := by
      simpa only [runUnits] using result
    pure result'

private theorem replay_evalLiteral {source target : World} (fuel : Nat) (env : Env)
    (expr : Expr) (literal : primitiveLiteral expr) :
    ReplayEquivalent (evalExpr fuel env expr : M source Val) (evalExpr fuel env expr : M target Val) := by
  cases fuel with
  | zero => exact replay_outOfFuel
  | succ n =>
    cases expr with
    | undef | null | bool _ | num _ | str _ => exact replay_pure _
    | _ => cases literal

private theorem replay_execLiteralDeclaration {source target : World} (fuel : Nat) (env : Env)
    (stmt : Stmt) (literal : literalDeclaration stmt) :
    ReplayEquivalent (execStmt fuel env stmt : M source Flow) (execStmt fuel env stmt : M target Flow) := by
  rcases literal with ⟨kind, name, init, rfl, initLiteral⟩
  cases fuel with
  | zero => exact replay_outOfFuel
  | succ fuel =>
    cases kind with
    | var =>
      unfold execStmt
      apply replay_bind (replay_evalLiteral fuel env init initLiteral)
      intro value
      apply replay_bind (replay_writeVar env name value)
      intro _
      exact replay_pure Flow.normal
    | let_ | const =>
      unfold execStmt
      apply replay_bind (replay_evalLiteral fuel env init initLiteral)
      intro value
      apply replay_bind (replay_initVar env name value)
      intro _
      exact replay_pure Flow.normal

/-- Transport a primitive-literal declaration across worlds. Its expression
does not inspect the world, and writing the already-linked cell preserves the
world component. -/
private theorem runUnit_literalDeclaration {source target : World} (fuel : Nat) (env : Env) (id : Nat)
    (stmt : Stmt) (literal : literalDeclaration stmt) (s : State source) :
    runUnit (W := target) fuel env id stmt (replaceWorld target s) =
      transportResult target (runUnit (W := source) fuel env id stmt s) := by
  exact (replay_inRegion id (replay_execLiteralDeclaration fuel env stmt literal)) s

private theorem runUnit_replayable {source target : World} (fuel : Nat) (env : Env) (id : Nat)
    (stmt : Stmt) (supported : replayableStmt env stmt) (s : State source) :
    runUnit (W := target) fuel env id stmt (replaceWorld target s) =
      transportResult target (runUnit (W := source) fuel env id stmt s) := by
  exact (replay_inRegion id (replay_execReplayable fuel env stmt supported)) s

private theorem runUnits_replayable {source target : World} (fuel : Nat)
    (units : List LinkedUnit) (supported : ReplayableSchedule units) (s : State source) :
    runUnits (W := target) fuel units (replaceWorld target s) =
      transportResult target (runUnits (W := source) fuel units s) := by
  induction units generalizing s with
  | nil => rfl
  | cons unit rest ih =>
    obtain ⟨id, stmt, env⟩ := unit
    have unitSupported := supported (id, stmt, env) (by simp)
    have restSupported : ReplayableSchedule rest := by
      intro next member
      exact supported next (by simp [member])
    unfold runUnits
    simp only [M.bind_apply]
    rw [runUnit_replayable fuel env id stmt unitSupported s]
    cases runUnit (W := source) fuel env id stmt s with
    | ok flow next =>
      simp only [transportResult]
      exact ih restSupported next
    | throw value next => rfl
    | unknown why next => rfl
    | fuel => rfl

/-- Check the operations reached by the concrete probe. Own data reads are
checked against the current heap; unsupported reads retain the fallback. -/
def replayPremisedDynamicWitness {premises : HostPremises} {p : Program} (fuel : Nat) (allowLogical : Bool)
    (checked : CheckedLink p World.probe) :
    Option (PLift (∀ probeState,
      runProgram (premises.specialize World.probe) fuel p = .ok () probeState →
      ∀ w, runProgram (premises.specialize w) fuel p =
        .ok () (replaceWorld (premises.specialize w) probeState))) := do
  let source := premises.specialize World.probe
  let sourceChecked : CheckedLink p source :=
    ⟨checked.linked, linkState source checked.ready, link_from_probe checked source⟩
  let units := unitsOf checked.linked
  let safeLogical := allowLogical && units.all (fun unit => primitiveLogicalStmt unit.stmt)
  let run ← checkReplayUnits safeLogical fuel units sourceChecked.ready
  return ⟨by
    intro probeState probeRun w
    let target := premises.specialize w
    let targetChecked : CheckedLink p target :=
      ⟨checked.linked, linkState target checked.ready, link_from_probe checked target⟩
    rw [run_after_link fuel sourceChecked] at probeRun
    have finalState : run.next = probeState := by
      have same := run.run.symm.trans probeRun
      cases same
      rfl
    have ready : replaceWorld target sourceChecked.ready = targetChecked.ready := rfl
    rw [run_after_link fuel targetChecked, ← ready, ← finalState]
    simpa using run.replay target⟩

/-- A successful probe link transports to each world without executing any
host operation. -/
theorem replayPremised_hoisted {premises : HostPremises} {p : Program} {fuel : Nat}
    {probeState : State (HostPremises.specialize premises World.probe)}
    (checked : CheckedLink p World.probe)
    (hoisted : HoistedSchedule (unitsOf checked.linked))
    (probeRun : runProgram (HostPremises.specialize premises World.probe) fuel p = .ok () probeState)
    (w : World) :
    runProgram (HostPremises.specialize premises w) fuel p =
      .ok () (replaceWorld (HostPremises.specialize premises w) probeState) := by
  let probeChecked : CheckedLink p (HostPremises.specialize premises World.probe) :=
    ⟨checked.linked, linkState (HostPremises.specialize premises World.probe) checked.ready,
      link_from_probe checked (HostPremises.specialize premises World.probe)⟩
  let worldChecked : CheckedLink p (HostPremises.specialize premises w) :=
    ⟨checked.linked, linkState (HostPremises.specialize premises w) checked.ready,
      link_from_probe checked (HostPremises.specialize premises w)⟩
  have probeAfterLink := run_after_link fuel probeChecked
  have worldAfterLink := run_after_link fuel worldChecked
  have probeUnitsEq : unitsOf probeChecked.linked = unitsOf checked.linked := rfl
  have worldUnitsEq : unitsOf worldChecked.linked = unitsOf checked.linked := rfl
  have probeHoisted : HoistedSchedule (unitsOf probeChecked.linked) := by
    rw [probeUnitsEq]
    exact hoisted
  have worldHoisted : HoistedSchedule (unitsOf worldChecked.linked) := by
    rw [worldUnitsEq]
    exact hoisted
  have probeUnits := runUnits_hoisted fuel (unitsOf probeChecked.linked) probeHoisted probeChecked.ready
  have worldUnits := runUnits_hoisted fuel (unitsOf worldChecked.linked) worldHoisted worldChecked.ready
  rw [probeAfterLink, probeUnits] at probeRun
  by_cases exhausted : fuel = 0 ∧ unitsOf checked.linked ≠ []
  · have probeExhausted : fuel = 0 ∧ unitsOf probeChecked.linked ≠ [] := by
      rw [probeUnitsEq]
      exact exhausted
    simp [probeExhausted] at probeRun
  · have probeStateEq : probeState = finishHoistedState (unitsOf checked.linked) probeChecked.ready := by
      have runEq : Res.ok () probeState = Res.ok () (finishHoistedState (unitsOf checked.linked) probeChecked.ready) := by
        cases fuel with
        | zero =>
          have unitsEmpty : unitsOf checked.linked = [] := by
            cases hUnits : unitsOf checked.linked with
            | nil => rfl
            | cons unit rest =>
              exfalso
              apply exhausted
              exact ⟨rfl, by simp [hUnits]⟩
          simpa [probeUnitsEq, unitsEmpty] using probeRun.symm
        | succ n =>
          simpa [probeUnitsEq] using probeRun.symm
      injection runEq with _ stateEq
    have worldNotExhausted : ¬ (fuel = 0 ∧ unitsOf worldChecked.linked ≠ []) := by
      simpa [worldUnitsEq] using exhausted
    have mapReady : replaceWorld (HostPremises.specialize premises w) probeChecked.ready = worldChecked.ready := rfl
    rw [worldAfterLink, worldUnits]
    simp only [if_neg worldNotExhausted]
    rw [probeStateEq, replaceWorld_finishHoisted, mapReady]

/-- A probe run of a replayable schedule is its run in every world, with only
the world's state replaced by that world's initial state. The supported
schedule includes hoisted declarations and primitive-literal initializers. -/
theorem replayPremised {premises : HostPremises} {p : Program} {fuel : Nat}
    {probeState : State (HostPremises.specialize premises World.probe)}
    (checked : CheckedLink p World.probe)
    (supported : ReplayableSchedule (unitsOf checked.linked))
    (probeRun : runProgram (HostPremises.specialize premises World.probe) fuel p = .ok () probeState)
    (w : World) :
    runProgram (HostPremises.specialize premises w) fuel p =
      .ok () (replaceWorld (HostPremises.specialize premises w) probeState) := by
  let source := HostPremises.specialize premises World.probe
  let target := HostPremises.specialize premises w
  let sourceChecked : CheckedLink p source :=
    ⟨checked.linked, linkState source checked.ready, link_from_probe checked source⟩
  let targetChecked : CheckedLink p target :=
    ⟨checked.linked, linkState target checked.ready, link_from_probe checked target⟩
  have sourceRun := probeRun
  rw [run_after_link fuel sourceChecked] at sourceRun
  have targetUnits := runUnits_replayable (source := source) (target := target) fuel
    (unitsOf targetChecked.linked) supported sourceChecked.ready
  have readyEq : replaceWorld target sourceChecked.ready = targetChecked.ready := rfl
  rw [run_after_link fuel targetChecked, ← readyEq, targetUnits, sourceRun]
  rfl

/-- Equal successful probe runs of two replayable schedules remain equal in
every specialized world. -/
theorem replayPremised_pair {premises : HostPremises} {source candidate : Program} {fuel : Nat}
    {sourceState candidateState : State (premises.specialize World.probe)}
    (sourceChecked : CheckedLink source World.probe)
    (candidateChecked : CheckedLink candidate World.probe)
    (sourceSupported : ReplayableSchedule (unitsOf sourceChecked.linked))
    (candidateSupported : ReplayableSchedule (unitsOf candidateChecked.linked))
    (sourceProbeRun : runProgram (premises.specialize World.probe) fuel source = .ok () sourceState)
    (candidateProbeRun : runProgram (premises.specialize World.probe) fuel candidate = .ok () candidateState)
    (statesAgree : sourceState = candidateState) (w : World) :
    runProgram (premises.specialize w) fuel source =
        .ok () (replaceWorld (premises.specialize w) sourceState) ∧
      runProgram (premises.specialize w) fuel candidate =
        .ok () (replaceWorld (premises.specialize w) sourceState) := by
  have sourceReplay := replayPremised sourceChecked sourceSupported sourceProbeRun w
  have candidateReplay := replayPremised candidateChecked candidateSupported candidateProbeRun w
  constructor
  · exact sourceReplay
  · rw [statesAgree]
    exact candidateReplay

end CoreJS.LinkedBehavior

#print axioms CoreJS.LinkedBehavior.replayPremised_hoisted
#print axioms CoreJS.LinkedBehavior.replayPremised
#print axioms CoreJS.LinkedBehavior.replayPremised_pair
#print axioms CoreJS.LinkedBehavior.replayPremisedDynamicWitness
#print axioms CoreJS.LinkedBehavior.evalExpr_void
