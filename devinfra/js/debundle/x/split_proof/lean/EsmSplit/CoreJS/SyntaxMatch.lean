import EsmSplit.CoreJS.Syntax

namespace CoreJS.SyntaxMatch

-- Lean cannot derive DecidableEq for nested mutual inductives. Compare the constructors
-- produced by LinkedInput; every successful branch returns equality, including deferred code.
abbrev MatchEq (α : Type) := (a b : α) → Option (PLift (a = b))

def scalarEq [DecidableEq α] : MatchEq α := fun a b =>
  if h : a = b then some ⟨h⟩ else none

def listEq (item : MatchEq α) : MatchEq (List α)
  | [], [] => some ⟨rfl⟩
  | x :: xs, y :: ys => do
    let ⟨head⟩ ← item x y
    let ⟨tail⟩ ← listEq item xs ys
    return ⟨head ▸ tail ▸ rfl⟩
  | _, _ => none

def optionEq (item : MatchEq α) : MatchEq (Option α)
  | none, none => some ⟨rfl⟩
  | some a, some b => (item a b).map fun h => ⟨congrArg some h.down⟩
  | _, _ => none

mutual
partial def exprEq : MatchEq Expr
  | .undef, .undef | .null, .null | .this, .this => some ⟨rfl⟩
  | .regexp a, .regexp b => (scalarEq a b).map fun h => ⟨congrArg Expr.regexp h.down⟩
  | .str a, .str b => (scalarEq a b).map fun h => ⟨congrArg Expr.str h.down⟩
  | .num a, .num b => (scalarEq a b).map fun h => ⟨congrArg Expr.num h.down⟩
  | .bool a, .bool b => (scalarEq a b).map fun h => ⟨congrArg Expr.bool h.down⟩
  | .var a, .var b => (scalarEq a b).map fun h => ⟨congrArg Expr.var h.down⟩
  | .host a, .host b => (scalarEq a b).map fun h => ⟨congrArg Expr.host h.down⟩
  | .obj a, .obj b => (listEq propEq a b).map fun h => ⟨congrArg Expr.obj h.down⟩
  | .arr a, .arr b => (listEq exprEq a b).map fun h => ⟨congrArg Expr.arr h.down⟩
  | .arrWithSpread a, .arrWithSpread b =>
    (listEq arrayElementEq a b).map fun h => ⟨congrArg Expr.arrWithSpread h.down⟩
  | .prim op args, .prim op' args' => do
    let ⟨operator⟩ ← scalarEq op op'
    let ⟨arguments⟩ ← listEq exprEq args args'
    return ⟨operator ▸ arguments ▸ rfl⟩
  | .unary op a, .unary op' b => do
    let ⟨o⟩ ← scalarEq op op'
    let ⟨v⟩ ← exprEq a b
    return ⟨o ▸ v ▸ rfl⟩
  | .binary op a b, .binary op' a' b' => do
    let ⟨o⟩ ← scalarEq op op'
    let ⟨left⟩ ← exprEq a a'
    let ⟨right⟩ ← exprEq b b'
    return ⟨o ▸ left ▸ right ▸ rfl⟩
  | .logical op a b, .logical op' a' b' => do
    let ⟨o⟩ ← scalarEq op op'
    let ⟨left⟩ ← exprEq a a'
    let ⟨right⟩ ← exprEq b b'
    return ⟨o ▸ left ▸ right ▸ rfl⟩
  | .cond c t e, .cond c' t' e' => do
    let ⟨test⟩ ← exprEq c c'
    let ⟨yes⟩ ← exprEq t t'
    let ⟨no⟩ ← exprEq e e'
    return ⟨test ▸ yes ▸ no ▸ rfl⟩
  | .seq a b, .seq a' b' => do
    let ⟨left⟩ ← exprEq a a'
    let ⟨right⟩ ← exprEq b b'
    return ⟨left ▸ right ▸ rfl⟩
  | .hasProp k o, .hasProp k' o' => do
    let ⟨key⟩ ← exprEq k k'
    let ⟨base⟩ ← exprEq o o'
    return ⟨key ▸ base ▸ rfl⟩
  | .get o k, .get o' k' => do
    let ⟨base⟩ ← exprEq o o'
    let ⟨key⟩ ← exprEq k k'
    return ⟨base ▸ key ▸ rfl⟩
  | .getSuper k, .getSuper k' => do
    let ⟨key⟩ ← exprEq k k'
    return ⟨key ▸ rfl⟩
  | .getOptional o k, .getOptional o' k' => do
    let ⟨base⟩ ← exprEq o o'
    let ⟨key⟩ ← exprEq k k'
    return ⟨base ▸ key ▸ rfl⟩
  | .delete o k, .delete o' k' => do
    let ⟨base⟩ ← exprEq o o'
    let ⟨key⟩ ← exprEq k k'
    return ⟨base ▸ key ▸ rfl⟩
  | .assign t rhs, .assign t' rhs' => do
    let ⟨target⟩ ← targetEq t t'
    let ⟨value⟩ ← exprEq rhs rhs'
    return ⟨target ▸ value ▸ rfl⟩
  | .assignOp t op rhs, .assignOp t' op' rhs' => do
    let ⟨target⟩ ← targetEq t t'
    let ⟨operator⟩ ← scalarEq op op'
    let ⟨value⟩ ← exprEq rhs rhs'
    return ⟨target ▸ operator ▸ value ▸ rfl⟩
  | .assignLogical t op rhs, .assignLogical t' op' rhs' => do
    let ⟨target⟩ ← targetEq t t'
    let ⟨operator⟩ ← scalarEq op op'
    let ⟨value⟩ ← exprEq rhs rhs'
    return ⟨target ▸ operator ▸ value ▸ rfl⟩
  | .update t inc pre, .update t' inc' pre' => do
    let ⟨target⟩ ← targetEq t t'
    let ⟨increment⟩ ← scalarEq inc inc'
    let ⟨isPrefix⟩ ← scalarEq pre pre'
    return ⟨target ▸ increment ▸ isPrefix ▸ rfl⟩
  | .callMethod o k args, .callMethod o' k' args' => do
    let ⟨base⟩ ← exprEq o o'
    let ⟨key⟩ ← exprEq k k'
    let ⟨arguments⟩ ← listEq exprEq args args'
    return ⟨base ▸ key ▸ arguments ▸ rfl⟩
  | .callSuper k args optional, .callSuper k' args' optional' => do
    let ⟨key⟩ ← exprEq k k'
    let ⟨xs⟩ ← listEq exprEq args args'
    let ⟨opt⟩ ← scalarEq optional optional'
    return ⟨key ▸ xs ▸ opt ▸ rfl⟩
  | .callOptionalReceiver o k args, .callOptionalReceiver o' k' args' => do
    let ⟨base⟩ ← exprEq o o'
    let ⟨key⟩ ← exprEq k k'
    let ⟨arguments⟩ ← listEq exprEq args args'
    return ⟨base ▸ key ▸ arguments ▸ rfl⟩
  | .callOptionalCallee o k args, .callOptionalCallee o' k' args' => do
    let ⟨base⟩ ← exprEq o o'
    let ⟨key⟩ ← exprEq k k'
    let ⟨arguments⟩ ← listEq exprEq args args'
    return ⟨base ▸ key ▸ arguments ▸ rfl⟩
  | .cls n self ctor ms, .cls n' self' ctor' ms' => do
    let ⟨k⟩ ← scalarEq n n'
    let ⟨s⟩ ← scalarEq self self'
    let ⟨c⟩ ← funcEq ctor ctor'
    let ⟨m⟩ ← listEq classMemberEq ms ms'
    return ⟨k ▸ s ▸ c ▸ m ▸ rfl⟩
  | .derived n self parent ms ctor, .derived n' self' parent' ms' ctor' => do
    let ⟨k⟩ ← scalarEq n n'
    let ⟨s⟩ ← scalarEq self self'
    let ⟨p⟩ ← exprEq parent parent'
    let ⟨m⟩ ← listEq classMemberEq ms ms'
    let ⟨c⟩ ← derivedCtorEq ctor ctor'
    return ⟨k ▸ s ▸ p ▸ m ▸ c ▸ rfl⟩
  | .new f args, .new f' args' => do
    let ⟨callee⟩ ← exprEq f f'
    let ⟨arguments⟩ ← listEq exprEq args args'
    return ⟨callee ▸ arguments ▸ rfl⟩
  | .call f args, .call f' args' => do
    let ⟨callee⟩ ← exprEq f f'
    let ⟨arguments⟩ ← listEq exprEq args args'
    return ⟨callee ▸ arguments ▸ rfl⟩
  | .func a, .func b => (funcEq a b).map fun h => ⟨congrArg Expr.func h.down⟩
  | _, _ => none

private partial def targetEq : MatchEq Target
  | .var x, .var x' => (scalarEq x x').map fun h => ⟨congrArg Target.var h.down⟩
  | .frozen x, .frozen x' => (scalarEq x x').map fun h => ⟨congrArg Target.frozen h.down⟩
  | .member o k, .member o' k' => do
    let ⟨base⟩ ← exprEq o o'
    let ⟨key⟩ ← exprEq k k'
    return ⟨base ▸ key ▸ rfl⟩
  | _, _ => none

private partial def arrayElementEq : MatchEq (ArrayElementKind × Expr)
  | (kind, e), (kind', e') => do
    let ⟨tag⟩ ← scalarEq kind kind'
    let ⟨value⟩ ← exprEq e e'
    return ⟨tag ▸ value ▸ rfl⟩

private partial def derivedCtorEq : MatchEq (Option (List Stmt × Option (List Expr) × Func))
  | none, none => some ⟨rfl⟩
  | some (before, args, f), some (before', args', g) => do
    let ⟨p⟩ ← listEq stmtEq before before'
    let ⟨a⟩ ← optionEq (listEq exprEq) args args'
    let ⟨b⟩ ← funcEq f g
    return ⟨p ▸ a ▸ b ▸ rfl⟩
  | _, _ => none

partial def funcEq : MatchEq Func
  | .mk name kind self params body, .mk name' kind' self' params' body' => do
    let ⟨n⟩ ← scalarEq name name'
    let ⟨k⟩ ← scalarEq kind kind'
    let ⟨s⟩ ← scalarEq self self'
    let ⟨p⟩ ← scalarEq params params'
    let ⟨b⟩ ← listEq stmtEq body body'
    return ⟨n ▸ k ▸ s ▸ p ▸ b ▸ rfl⟩
  | .withDefaults name kind self params arity body, .withDefaults name' kind' self' params' arity' body' => do
    let ⟨n⟩ ← scalarEq name name'
    let ⟨k⟩ ← scalarEq kind kind'
    let ⟨s⟩ ← scalarEq self self'
    let ⟨p⟩ ← scalarEq params params'
    let ⟨a⟩ ← scalarEq arity arity'
    let ⟨b⟩ ← listEq stmtEq body body'
    return ⟨n ▸ k ▸ s ▸ p ▸ a ▸ b ▸ rfl⟩
  | .opaque name kind self arity code part free refusal,
      .opaque name' kind' self' arity' code' part' free' refusal' => do
    let ⟨n⟩ ← scalarEq name name'
    let ⟨k⟩ ← scalarEq kind kind'
    let ⟨s⟩ ← scalarEq self self'
    let ⟨a⟩ ← scalarEq arity arity'
    let ⟨c⟩ ← scalarEq code code'
    let ⟨p⟩ ← scalarEq part part'
    let ⟨f⟩ ← scalarEq free free'
    let ⟨r⟩ ← scalarEq refusal refusal'
    return ⟨n ▸ k ▸ s ▸ a ▸ c ▸ p ▸ f ▸ r ▸ rfl⟩
  | _, _ => none

private partial def classMemberEq : MatchEq ClassMember
  | .field s k v, .field s' k' v' => do
    let ⟨st⟩ ← scalarEq s s'
    let ⟨key⟩ ← exprEq k k'
    let ⟨value⟩ ← exprEq v v'
    return ⟨st ▸ key ▸ value ▸ rfl⟩
  | .method s k v, .method s' k' v' => do
    let ⟨st⟩ ← scalarEq s s'
    let ⟨key⟩ ← exprEq k k'
    let ⟨value⟩ ← funcEq v v'
    return ⟨st ▸ key ▸ value ▸ rfl⟩
  | .getter s k v, .getter s' k' v' => do
    let ⟨st⟩ ← scalarEq s s'
    let ⟨key⟩ ← exprEq k k'
    let ⟨value⟩ ← funcEq v v'
    return ⟨st ▸ key ▸ value ▸ rfl⟩
  | .setter s k v, .setter s' k' v' => do
    let ⟨st⟩ ← scalarEq s s'
    let ⟨key⟩ ← exprEq k k'
    let ⟨value⟩ ← funcEq v v'
    return ⟨st ▸ key ▸ value ▸ rfl⟩
  | _, _ => none

private partial def propEq : MatchEq PropDef
  | .init k v, .init k' v' => do
    let ⟨key⟩ ← exprEq k k'
    let ⟨value⟩ ← exprEq v v'
    return ⟨key ▸ value ▸ rfl⟩
  | .method k f, .method k' f' => do
    let ⟨key⟩ ← exprEq k k'
    let ⟨method⟩ ← funcEq f f'
    return ⟨key ▸ method ▸ rfl⟩
  | .getter k f, .getter k' f' => do
    let ⟨key⟩ ← exprEq k k'
    let ⟨fn⟩ ← funcEq f f'
    return ⟨key ▸ fn ▸ rfl⟩
  | .setter k f, .setter k' f' => do
    let ⟨key⟩ ← exprEq k k'
    let ⟨fn⟩ ← funcEq f f'
    return ⟨key ▸ fn ▸ rfl⟩
  | .proto v, .proto v' => (exprEq v v').map fun h => ⟨congrArg PropDef.proto h.down⟩
  | .spread a, .spread b => (exprEq a b).map fun h => ⟨congrArg PropDef.spread h.down⟩
  | _, _ => none

/-- A match returns a proof of statement equality. -/
partial def stmtEq : MatchEq Stmt
  | .funDecl a, .funDecl b => (funcEq a b).map fun h => ⟨congrArg Stmt.funDecl h.down⟩
  | .varDecl a, .varDecl b => (scalarEq a b).map fun h => ⟨congrArg Stmt.varDecl h.down⟩
  | .expr a, .expr b => (exprEq a b).map fun h => ⟨congrArg Stmt.expr h.down⟩
  | .ret a, .ret b => (exprEq a b).map fun h => ⟨congrArg Stmt.ret h.down⟩
  | .throw a, .throw b => (exprEq a b).map fun h => ⟨congrArg Stmt.throw h.down⟩
  | .ite c t e, .ite c' t' e' => do
    let ⟨test⟩ ← exprEq c c'
    let ⟨yes⟩ ← stmtEq t t'
    let ⟨no⟩ ← stmtEq e e'
    return ⟨by rw [test, yes, no]⟩
  | .block a, .block b => (listEq stmtEq a b).map fun h => ⟨congrArg Stmt.block h.down⟩
  | .brk, .brk | .cont, .cont => some ⟨rfl⟩
  | .loop init test update body, .loop init' test' update' body' => do
    let ⟨i⟩ ← stmtEq init init'
    let ⟨t⟩ ← exprEq test test'
    let ⟨u⟩ ← exprEq update update'
    let ⟨b⟩ ← stmtEq body body'
    return ⟨i ▸ t ▸ u ▸ b ▸ rfl⟩
  | .tryCatch body param handler, .tryCatch body' param' handler' => do
    let ⟨b⟩ ← stmtEq body body'
    let ⟨p⟩ ← scalarEq param param'
    let ⟨h⟩ ← stmtEq handler handler'
    return ⟨b ▸ p ▸ h ▸ rfl⟩
  | .tryFinally body finalizer, .tryFinally body' finalizer' => do
    let ⟨b⟩ ← stmtEq body body'
    let ⟨f⟩ ← stmtEq finalizer finalizer'
    return ⟨b ▸ f ▸ rfl⟩
  | .decl kind name init, .decl kind' name' init' => do
    let ⟨k⟩ ← scalarEq kind kind'
    let ⟨n⟩ ← scalarEq name name'
    let ⟨v⟩ ← exprEq init init'
    return ⟨k ▸ n ▸ v ▸ rfl⟩
  | .opaque code decls funs free run, .opaque code' decls' funs' free' run' => do
    let ⟨c⟩ ← scalarEq code code'
    let ⟨d⟩ ← scalarEq decls decls'
    let ⟨f⟩ ← scalarEq funs funs'
    let ⟨n⟩ ← scalarEq free free'
    let ⟨r⟩ ← scalarEq run run'
    return ⟨c ▸ d ▸ f ▸ n ▸ r ▸ rfl⟩
  | _, _ => none
end

private def unitEq : MatchEq (Nat × Stmt)
  | (i, s), (j, t) => do
    let ⟨id⟩ ← scalarEq i j
    let ⟨stmt⟩ ← stmtEq s t
    return ⟨id ▸ stmt ▸ rfl⟩

private def moduleEq : MatchEq Module
  | ⟨n, i, b, e⟩, ⟨n', i', b', e'⟩ => do
    let ⟨name⟩ ← scalarEq n n'
    let ⟨imports⟩ ← scalarEq i i'
    let ⟨body⟩ ← listEq unitEq b b'
    let ⟨exports⟩ ← scalarEq e e'
    return ⟨name ▸ imports ▸ body ▸ exports ▸ rfl⟩

/-- A match returns a proof of linked program equality. -/
def programEq : MatchEq Program
  | ⟨e, m⟩, ⟨e', m'⟩ => do
    let ⟨entry⟩ ← scalarEq e e'
    let ⟨modules⟩ ← listEq moduleEq m m'
    return ⟨entry ▸ modules ▸ rfl⟩

end CoreJS.SyntaxMatch
