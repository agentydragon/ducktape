/- Shared execution state and transitions for the supported subsets. -/
inductive Observation where
  | text (value : String)
  | number (value : Nat)
  deriving DecidableEq, Repr

inductive ThrownValue where
  | number (value : Nat)
  | text (value : String)
  | boolean (value : Bool)
  | null
  | undefined
  deriving DecidableEq, Repr

/- Expressions are evaluated by the same transition system as the original
   operations. Parameters shadow module bindings; arithmetic is restricted by
   the reported JavaScript safe-integer assumption, excluding negative zero.
   Remainder by zero and non-integral or zero-divisor division are outside
   the modeled numeric subset. -/
inductive BitOp where
  | and | or | xor
  deriving DecidableEq, Repr

inductive NumericExpr where
  | literal (value : Nat)
  | name (name : String)
  | add (left right : NumericExpr)
  | multiply (left right : NumericExpr)
  | subtract (left right : NumericExpr)
  | remainder (left right : NumericExpr)
  | divide (left right : NumericExpr)
  | bitAnd (left right : NumericExpr)
  | bitOr (left right : NumericExpr)
  | bitXor (left right : NumericExpr)
  | unsignedShift (left right : NumericExpr)
  | shiftLeft (left right : NumericExpr)
  | shiftRight (left right : NumericExpr)
  | equal (left right : NumericExpr)
  | less (left right : NumericExpr)
  | greater (left right : NumericExpr)
  | conditional (test consequent alternative : NumericExpr)
  | guard (test consequent continuation : NumericExpr)
  | finally (body finalizer : NumericExpr)
  | catchError (body handler : NumericExpr)
  | followedBy (first continuation : NumericExpr)
  | local (name : String) (mutable : Bool) (initial body : NumericExpr)
  | assign (name : String) (value body : NumericExpr)
  | raise (value : NumericExpr)
  | raisePrimitive (value : ThrownValue)
  | argumentsNil
  | argumentsCons (head tail : NumericExpr)
  | invoke (name : String) (parameters : List String) (arguments body : NumericExpr)
  | invokeLocal (name : String) (parameters : List String) (arguments body : NumericExpr)
  deriving DecidableEq, Repr

inductive Operation where
  | initialize (value : Nat)
  | increment
  | captureSnapshot
  | incrementSnapshot
  | readCell
  | readSnapshot
  | recordText (value : String)
  | recordCell
  | declareBinding (name : String) (value : Nat)
  | copyBinding (target source : String)
  | incrementBinding (name : String)
  | addBinding (target source : String)
  | observeBinding (name : String)
  | readBinding (name : String)
  | returnBinding (name : String)
  | returnUndefined
  | throwNumber (value : Nat)
  | returnExpression (expression : NumericExpr) (parameters : List (String × Nat))
  | initializeCapture (key : String) (expression : NumericExpr) (parameters : List (String × Nat)) (prior : List (String × String))
  | invokeCaptured (captures : List (String × String)) (parameters : List String) (arguments : List Nat) (body : NumericExpr)
  deriving DecidableEq, Repr

structure ExecutionState (α : Type) where
  bindings : List (String × Nat)
  events : List Observation
  replies : List (Option α)
  error : Option α
  returns : List (Option Nat)
  thrown : Option ThrownValue
  captured : List (String × Nat)
  deriving DecidableEq, Repr

def lookupBinding : List (String × Nat) → String → Option Nat
  | [], _ => none
  | (key, value) :: rest, name => if key == name then some value else lookupBinding rest name

def updateBinding : List (String × Nat) → String → Nat → Option (List (String × Nat))
  | [], _, _ => none
  | (key, old) :: rest, name, value =>
    if key == name then some ((key, value) :: rest)
    else (updateBinding rest name value).map (fun tail => (key, old) :: tail)

/- A successful lookup witnesses an existing cell. Updating that cell succeeds
   and the same key subsequently resolves to the new value. -/
theorem update_existing_cell (store : List (String × Nat)) (name : String) (old value : Nat)
    (found : lookupBinding store name = some old) :
    ∃ updated, updateBinding store name value = some updated ∧ lookupBinding updated name = some value := by
  induction store with
  | nil => cases found
  | cons entry rest ih =>
    cases entry with
    | mk key previous =>
      change (if key == name then some previous else lookupBinding rest name) = some old at found
      change ∃ updated, (if key == name then some ((key, value) :: rest)
        else (updateBinding rest name value).map (fun tail => (key, previous) :: tail)) = some updated ∧
        lookupBinding updated name = some value
      cases matchesKey : key == name with
      | true =>
        refine ⟨(key, value) :: rest, rfl, ?_⟩
        change (if key == name then some value else lookupBinding rest name) = some value
        rw [matchesKey]
        rfl
      | false =>
        rw [matchesKey] at found
        obtain ⟨tail, updatedTail, foundTail⟩ := ih found
        rw [updatedTail]
        refine ⟨(key, previous) :: tail, rfl, ?_⟩
        change (if key == name then some previous else lookupBinding tail name) = some value
        rw [matchesKey]
        exact foundTail

theorem lookup_update_other (store updated : List (String × Nat)) (target other : String) (value : Nat)
    (different : target ≠ other) (updatedStore : updateBinding store target value = some updated) :
    lookupBinding updated other = lookupBinding store other := by
  induction store generalizing updated with
  | nil =>
    cases updatedStore
  | cons entry rest inductionHypothesis =>
    cases entry with
    | mk key old =>
      change (if key == target then some ((key, value) :: rest)
        else (updateBinding rest target value).map (fun tail => (key, old) :: tail)) = some updated at updatedStore
      cases keyIsTarget : key == target with
      | true =>
        rw [keyIsTarget] at updatedStore
        cases updatedStore
        have keyEqualsTarget : key = target := beq_iff_eq.mp keyIsTarget
        have keyDiffers : key ≠ other := fun equal => different (keyEqualsTarget.symm.trans equal)
        have keyIsNotOther : (key == other) = false := by
          cases keyIsOther : key == other with
          | false => rfl
          | true => exact False.elim (keyDiffers (beq_iff_eq.mp keyIsOther))
        change (if key == other then some value else lookupBinding rest other) =
          (if key == other then some old else lookupBinding rest other)
        rw [keyIsNotOther]
        rfl
      | false =>
        rw [keyIsTarget] at updatedStore
        cases tailUpdate : updateBinding rest target value with
        | none =>
          rw [tailUpdate] at updatedStore
          cases updatedStore
        | some tail =>
          rw [tailUpdate] at updatedStore
          change some ((key, old) :: tail) = some updated at updatedStore
          cases updatedStore
          change (if key == other then some old else lookupBinding tail other) =
            (if key == other then some old else lookupBinding rest other)
          cases keyIsOther : key == other with
          | true => rfl
          | false =>
            exact inductionHypothesis tail tailUpdate

/- `Except.error` is a JavaScript throw, while `none` means the model could
   not evaluate an unsupported or uninitialized operation. The local frame is
   returned so captured cells can be observed after a call. -/
structure ExpressionResult (α : Type) where
  locals : List (String × Nat)
  completion : Except ThrownValue α

/- A bounded, axiom-free bitwise operation. Higher bits are discarded, as in
   JavaScript's ToInt32 coercion. -/
def bitwiseFuel : Nat → BitOp → Nat → Nat → Nat
  | 0, _, _, _ => 0
  | fuel + 1, op, left, right =>
    let done := match op with
      | .and => left == 0 || right == 0
      | .or | .xor => left == 0 && right == 0
    if done then 0 else
      let a := left % 2 == 1
      let b := right % 2 == 1
      let bit := match op with
        | .and => a && b
        | .or => a || b
        | .xor => a != b
      (if bit then 1 else 0) + 2 * bitwiseFuel fuel op (left / 2) (right / 2)

mutual
def evaluateExpression (locals globals : List (String × Nat)) : NumericExpr → Option (ExpressionResult Nat)
  | .literal value => some ⟨locals, .ok value⟩
  | .name name => do
    let value ← match lookupBinding locals name with
      | some value => some value
      | none => lookupBinding globals name
    pure ⟨locals, .ok value⟩
  | .add left right => do
    let leftResult ← evaluateExpression locals globals left
    match leftResult.completion with
    | .error value => pure ⟨leftResult.locals, .error value⟩
    | .ok leftValue =>
      let rightResult ← evaluateExpression leftResult.locals globals right
      match rightResult.completion with
      | .error value => pure ⟨rightResult.locals, .error value⟩
      | .ok rightValue => pure ⟨rightResult.locals, .ok (leftValue + rightValue)⟩
  | .equal left right => do
    let leftResult ← evaluateExpression locals globals left
    match leftResult.completion with
    | .error value => pure ⟨leftResult.locals, .error value⟩
    | .ok leftValue =>
      let rightResult ← evaluateExpression leftResult.locals globals right
      match rightResult.completion with
      | .error value => pure ⟨rightResult.locals, .error value⟩
      | .ok rightValue => pure ⟨rightResult.locals, .ok (if leftValue == rightValue then 1 else 0)⟩
  | .less left right => do
    let leftResult ← evaluateExpression locals globals left
    match leftResult.completion with
    | .error value => pure ⟨leftResult.locals, .error value⟩
    | .ok leftValue =>
      let rightResult ← evaluateExpression leftResult.locals globals right
      match rightResult.completion with
      | .error value => pure ⟨rightResult.locals, .error value⟩
      | .ok rightValue => pure ⟨rightResult.locals, .ok (if leftValue < rightValue then 1 else 0)⟩
  | .greater left right => do
    let leftResult ← evaluateExpression locals globals left
    match leftResult.completion with
    | .error value => pure ⟨leftResult.locals, .error value⟩
    | .ok leftValue =>
      let rightResult ← evaluateExpression leftResult.locals globals right
      match rightResult.completion with
      | .error value => pure ⟨rightResult.locals, .error value⟩
      | .ok rightValue => pure ⟨rightResult.locals, .ok (if leftValue > rightValue then 1 else 0)⟩
  | .multiply left right => do
    let leftResult ← evaluateExpression locals globals left
    match leftResult.completion with
    | .error value => pure ⟨leftResult.locals, .error value⟩
    | .ok leftValue =>
      let rightResult ← evaluateExpression leftResult.locals globals right
      match rightResult.completion with
      | .error value => pure ⟨rightResult.locals, .error value⟩
      | .ok rightValue => pure ⟨rightResult.locals, .ok (leftValue * rightValue)⟩
  | .bitAnd left right => do
    let leftResult ← evaluateExpression locals globals left
    match leftResult.completion with
    | .error value => pure ⟨leftResult.locals, .error value⟩
    | .ok leftValue =>
      let rightResult ← evaluateExpression leftResult.locals globals right
      match rightResult.completion with
      | .error value => pure ⟨rightResult.locals, .error value⟩
      | .ok rightValue =>
        let bits := bitwiseFuel 32 .and leftValue rightValue
        if bits < 2147483648 then pure ⟨rightResult.locals, .ok bits⟩ else none
  | .bitOr left right => do
    let leftResult ← evaluateExpression locals globals left
    match leftResult.completion with
    | .error value => pure ⟨leftResult.locals, .error value⟩
    | .ok leftValue =>
      let rightResult ← evaluateExpression leftResult.locals globals right
      match rightResult.completion with
      | .error value => pure ⟨rightResult.locals, .error value⟩
      | .ok rightValue =>
        let bits := bitwiseFuel 32 .or leftValue rightValue
        if bits < 2147483648 then pure ⟨rightResult.locals, .ok bits⟩ else none
  | .bitXor left right => do
    let leftResult ← evaluateExpression locals globals left
    match leftResult.completion with
    | .error value => pure ⟨leftResult.locals, .error value⟩
    | .ok leftValue =>
      let rightResult ← evaluateExpression leftResult.locals globals right
      match rightResult.completion with
      | .error value => pure ⟨rightResult.locals, .error value⟩
      | .ok rightValue =>
        let bits := bitwiseFuel 32 .xor leftValue rightValue
        if bits < 2147483648 then pure ⟨rightResult.locals, .ok bits⟩ else none
  | .unsignedShift left right => do
    let leftResult ← evaluateExpression locals globals left
    match leftResult.completion with
    | .error value => pure ⟨leftResult.locals, .error value⟩
    | .ok leftValue =>
      let rightResult ← evaluateExpression leftResult.locals globals right
      match rightResult.completion with
      | .error value => pure ⟨rightResult.locals, .error value⟩
      | .ok rightValue =>
        let bits := (leftValue % 4294967296) / (2 ^ (rightValue % 32))
        pure ⟨rightResult.locals, .ok bits⟩
  | .shiftLeft left right => do
    let leftResult ← evaluateExpression locals globals left
    match leftResult.completion with
    | .error value => pure ⟨leftResult.locals, .error value⟩
    | .ok leftValue =>
      let rightResult ← evaluateExpression leftResult.locals globals right
      match rightResult.completion with
      | .error value => pure ⟨rightResult.locals, .error value⟩
      | .ok rightValue =>
        let bits := (leftValue * 2 ^ (rightValue % 32)) % 4294967296
        if bits < 2147483648 then pure ⟨rightResult.locals, .ok bits⟩ else none
  | .shiftRight left right => do
    let leftResult ← evaluateExpression locals globals left
    match leftResult.completion with
    | .error value => pure ⟨leftResult.locals, .error value⟩
    | .ok leftValue =>
      let rightResult ← evaluateExpression leftResult.locals globals right
      match rightResult.completion with
      | .error value => pure ⟨rightResult.locals, .error value⟩
      | .ok rightValue =>
        let bits := leftValue % 4294967296
        if bits < 2147483648 then
          pure ⟨rightResult.locals, .ok (bits / (2 ^ (rightValue % 32)))⟩
        else none
  | .divide left right => do
    let leftResult ← evaluateExpression locals globals left
    match leftResult.completion with
    | .error value => pure ⟨leftResult.locals, .error value⟩
    | .ok leftValue =>
      let rightResult ← evaluateExpression leftResult.locals globals right
      match rightResult.completion with
      | .error value => pure ⟨rightResult.locals, .error value⟩
      | .ok rightValue =>
        if rightValue == 0 || leftValue % rightValue != 0 then none
        else pure ⟨rightResult.locals, .ok (leftValue / rightValue)⟩
  | .remainder left right => do
    let leftResult ← evaluateExpression locals globals left
    match leftResult.completion with
    | .error value => pure ⟨leftResult.locals, .error value⟩
    | .ok leftValue =>
      let rightResult ← evaluateExpression leftResult.locals globals right
      match rightResult.completion with
      | .error value => pure ⟨rightResult.locals, .error value⟩
      | .ok rightValue =>
        if rightValue == 0 then none
        else pure ⟨rightResult.locals, .ok (leftValue % rightValue)⟩
  | .subtract left right => do
    let leftResult ← evaluateExpression locals globals left
    match leftResult.completion with
    | .error value => pure ⟨leftResult.locals, .error value⟩
    | .ok leftValue =>
      let rightResult ← evaluateExpression leftResult.locals globals right
      match rightResult.completion with
      | .error value => pure ⟨rightResult.locals, .error value⟩
      | .ok rightValue =>
        if leftValue < rightValue then none
        else pure ⟨rightResult.locals, .ok (leftValue - rightValue)⟩
  | .conditional test consequent alternative => do
    let result ← evaluateExpression locals globals test
    match result.completion with
    | .error value => pure ⟨result.locals, .error value⟩
    | .ok condition =>
      if condition == 0 then evaluateExpression result.locals globals alternative
      else evaluateExpression result.locals globals consequent
  | .guard test consequent alternative => do
    let result ← evaluateExpression locals globals test
    match result.completion with
    | .error value => pure ⟨result.locals, .error value⟩
    | .ok condition =>
      if condition == 0 then evaluateExpression result.locals globals alternative
      else evaluateExpression result.locals globals consequent
  | .local name _ initial body => do
    let initialResult ← evaluateExpression locals globals initial
    match initialResult.completion with
    | .error value => pure ⟨initialResult.locals, .error value⟩
    | .ok value =>
      let bodyResult ← evaluateExpression ((name, value) :: initialResult.locals) globals body
      match bodyResult.locals with
      | [] => none
      | _ :: tail => pure ⟨tail, bodyResult.completion⟩
  | .catchError body handler => do
    let result ← evaluateExpression locals globals body
    match result.completion with
    | .error _ => evaluateExpression result.locals globals handler
    | .ok _ => pure result
  | .finally body finalizer => do
    let bodyResult ← evaluateExpression locals globals body
    let finalResult ← evaluateExpression bodyResult.locals globals finalizer
    match finalResult.completion with
    | .error thrown => pure ⟨finalResult.locals, .error thrown⟩
    | .ok _ => pure ⟨finalResult.locals, bodyResult.completion⟩
  | .followedBy first continuation => do
    let firstResult ← evaluateExpression locals globals first
    match firstResult.completion with
    | .error thrown => pure ⟨firstResult.locals, .error thrown⟩
    | .ok _ => evaluateExpression firstResult.locals globals continuation
  | .assign name expression body => do
    let result ← evaluateExpression locals globals expression
    match result.completion with
    | .error value => pure ⟨result.locals, .error value⟩
    | .ok value =>
      let updated ← updateBinding result.locals name value
      evaluateExpression updated globals body
  | .raise expression => do
    let result ← evaluateExpression locals globals expression
    match result.completion with
    | .error value => pure ⟨result.locals, .error value⟩
    | .ok value => pure ⟨result.locals, .error (.number value)⟩
  | .raisePrimitive value => some ⟨locals, .error value⟩
  | .argumentsNil => none
  | .argumentsCons _ _ => none
  | .invoke _ parameters arguments body => do
    let args ← evaluateArguments locals globals arguments
    match args.completion with
    | .error value => pure ⟨args.locals, .error value⟩
    | .ok values =>
      if parameters.length == values.length then
        (evaluateExpression (parameters.zip values) globals body).map (fun result => ⟨args.locals, result.completion⟩)
      else none
  | .invokeLocal _ parameters arguments body => do
    let args ← evaluateArguments locals globals arguments
    match args.completion with
    | .error value => pure ⟨args.locals, .error value⟩
    | .ok values =>
      if parameters.length == values.length then
        (evaluateExpression (parameters.zip values ++ args.locals) globals body).map
          (fun result => ⟨result.locals.drop parameters.length, result.completion⟩)
      else none

def evaluateArguments (locals globals : List (String × Nat)) : NumericExpr → Option (ExpressionResult (List Nat))
  | .argumentsNil => some ⟨locals, .ok []⟩
  | .argumentsCons head tail => do
    let headResult ← evaluateExpression locals globals head
    match headResult.completion with
    | .error value => pure ⟨headResult.locals, .error value⟩
    | .ok value =>
      let tailResult ← evaluateArguments headResult.locals globals tail
      match tailResult.completion with
      | .error error => pure ⟨tailResult.locals, .error error⟩
      | .ok values => pure ⟨tailResult.locals, .ok (value :: values)⟩
  | .literal _ => none
  | .name _ => none
  | .add _ _ => none
  | .multiply _ _ => none
  | .subtract _ _ => none
  | .remainder _ _ => none
  | .divide _ _ => none
  | .bitAnd _ _ => none
  | .bitOr _ _ => none
  | .bitXor _ _ => none
  | .unsignedShift _ _ => none
  | .shiftLeft _ _ => none
  | .shiftRight _ _ => none
  | .equal _ _ => none
  | .less _ _ => none
  | .greater _ _ => none
  | .conditional _ _ _ => none
  | .guard _ _ _ => none
  | .finally _ _ => none
  | .catchError _ _ => none
  | .followedBy _ _ => none
  | .local _ _ _ _ => none
  | .assign _ _ _ => none
  | .raise _ => none
  | .raisePrimitive _ => none
  | .invoke _ _ _ _ => none
  | .invokeLocal _ _ _ _ => none
end

/- Captured cells belong to the same execution state as module bindings, but
   have their own store. An environment maps lexical names to cell identities. -/
def loadCaptures (bindings : List (String × Nat)) : List (String × String) → Option (List (String × Nat))
  | [] => some []
  | (name, key) :: rest => do
    let value ← lookupBinding bindings key
    let tail ← loadCaptures bindings rest
    pure ((name, value) :: tail)

/- Environments correspond when each lexical name reads the same value,
   even if the two executions allocated that value under different cell keys. -/
def relatedCapturedEnvironments (leftStore rightStore : List (String × Nat))
    (leftCapture rightCapture : List (String × String)) : Prop :=
  ∃ frame, loadCaptures leftStore leftCapture = some frame ∧
    loadCaptures rightStore rightCapture = some frame

def decideRelatedCapturedEnvironments (leftStore rightStore : List (String × Nat))
    (leftCapture rightCapture : List (String × String)) :
    Decidable (relatedCapturedEnvironments leftStore rightStore leftCapture rightCapture) := by
  unfold relatedCapturedEnvironments
  cases leftResult : loadCaptures leftStore leftCapture with
  | none =>
    apply isFalse
    intro ⟨frame, impossible, right⟩
    cases impossible
  | some frame =>
    by_cases rightResult : loadCaptures rightStore rightCapture = some frame
    · exact isTrue ⟨frame, rfl, rightResult⟩
    · apply isFalse
      intro ⟨other, equal, right⟩
      cases equal
      exact rightResult right

instance (leftStore rightStore : List (String × Nat)) (leftCapture rightCapture : List (String × String)) :
    Decidable (relatedCapturedEnvironments leftStore rightStore leftCapture rightCapture) :=
  decideRelatedCapturedEnvironments leftStore rightStore leftCapture rightCapture

def evaluateCapturedFrame (globals store : List (String × Nat)) (captures : List (String × String))
    (parameters : List String) (arguments : List Nat) (body : NumericExpr) : Option (ExpressionResult Nat) := do
  if parameters.length != arguments.length then none else
    let values ← loadCaptures store captures
    evaluateExpression (parameters.zip arguments ++ values) globals body

theorem captured_frame_evaluation_preserved
    (globals leftStore rightStore : List (String × Nat)) (leftCapture rightCapture : List (String × String))
    (parameters : List String) (arguments : List Nat) (body : NumericExpr)
    (related : relatedCapturedEnvironments leftStore rightStore leftCapture rightCapture) :
    evaluateCapturedFrame globals leftStore leftCapture parameters arguments body =
      evaluateCapturedFrame globals rightStore rightCapture parameters arguments body := by
  obtain ⟨frame, left, right⟩ := related
  unfold evaluateCapturedFrame
  rw [left, right]

def storeCaptures (bindings : List (String × Nat)) : List (String × String) → List (String × Nat) → Option (List (String × Nat))
  | [], [] => some bindings
  | (name, key) :: rest, (actual, value) :: values => do
    if name != actual then none else
      let updated ← updateBinding bindings key value
      storeCaptures updated rest values
  | [], _ :: _ => none
  | _ :: _, [] => none

theorem lookup_store_other (captures : List (String × String)) (values : List (String × Nat))
    (store updated : List (String × Nat)) (other : String)
    (different : ∀ name key, (name, key) ∈ captures → key ≠ other)
    (stored : storeCaptures store captures values = some updated) :
    lookupBinding updated other = lookupBinding store other := by
  induction captures generalizing values store updated with
  | nil =>
    cases values with
    | nil =>
      change some store = some updated at stored
      cases stored
      rfl
    | cons value rest => cases stored
  | cons capture rest inductionHypothesis =>
    cases capture with
    | mk name key =>
      cases values with
      | nil => cases stored
      | cons entry entries =>
        cases entry with
        | mk actual value =>
          have headDifferent : key ≠ other := different name key (List.Mem.head rest)
          have tailDifferent : ∀ name key, (name, key) ∈ rest → key ≠ other := by
            intro tailName tailKey member
            exact different tailName tailKey (List.Mem.tail _ member)
          change (if name != actual then none else do
            let next ← updateBinding store key value
            storeCaptures next rest entries) = some updated at stored
          cases namesDiffer : name != actual with
          | true =>
            rw [namesDiffer] at stored
            cases stored
          | false =>
            rw [namesDiffer] at stored
            change (updateBinding store key value).bind (fun next => storeCaptures next rest entries) = some updated at stored
            cases update : updateBinding store key value with
            | none =>
              rw [update] at stored
              cases stored
            | some next =>
              rw [update] at stored
              change storeCaptures next rest entries = some updated at stored
              exact (inductionHypothesis entries next updated tailDifferent stored).trans
                (lookup_update_other store next key other value headDifferent update)

theorem load_store_disjoint (captures watched : List (String × String)) (values : List (String × Nat))
    (store updated : List (String × Nat))
    (different : ∀ name key, (name, key) ∈ captures →
      ∀ watchedName watchedKey, (watchedName, watchedKey) ∈ watched → key ≠ watchedKey)
    (stored : storeCaptures store captures values = some updated) :
    loadCaptures updated watched = loadCaptures store watched := by
  induction watched with
  | nil => rfl
  | cons watch rest inductionHypothesis =>
    cases watch with
    | mk watchedName watchedKey =>
      have headDifferent : ∀ name key, (name, key) ∈ captures → key ≠ watchedKey := by
        intro name key member
        exact different name key member watchedName watchedKey (List.Mem.head rest)
      have tailDifferent : ∀ name key, (name, key) ∈ captures →
          ∀ tailName tailKey, (tailName, tailKey) ∈ rest → key ≠ tailKey := by
        intro name key member tailName tailKey tailMember
        exact different name key member tailName tailKey (List.Mem.tail _ tailMember)
      have headEqual := lookup_store_other captures values store updated watchedKey headDifferent stored
      have tailEqual := inductionHypothesis tailDifferent
      change (do
        let value ← lookupBinding updated watchedKey
        let tail ← loadCaptures updated rest
        pure ((watchedName, value) :: tail)) = (do
        let value ← lookupBinding store watchedKey
        let tail ← loadCaptures store rest
        pure ((watchedName, value) :: tail))
      rw [headEqual, tailEqual]

/- Captures in one lexical environment must allocate distinct cells. -/
inductive CaptureKeysUnique : List (String × String) → Prop where
  | nil : CaptureKeysUnique []
  | cons (name key : String) (rest : List (String × String))
      (fresh : ∀ otherName otherKey, (otherName, otherKey) ∈ rest → key ≠ otherKey)
      (tail : CaptureKeysUnique rest) : CaptureKeysUnique ((name, key) :: rest)

def decideCaptureKeysUnique (captures : List (String × String)) : Decidable (CaptureKeysUnique captures) := by
  cases captures with
  | nil => exact isTrue .nil
  | cons head rest =>
    cases head with
    | mk name key =>
      let fresh : Prop := ∀ pair ∈ rest, key ≠ pair.2
      have freshDecidable : Decidable fresh := inferInstance
      cases freshDecidable with
      | isFalse absent =>
        apply isFalse
        intro unique
        cases unique with
        | cons _ _ _ distinct _ =>
          apply absent
          intro pair member
          exact distinct pair.1 pair.2 member
      | isTrue present =>
        cases decideCaptureKeysUnique rest with
        | isFalse absent =>
          apply isFalse
          intro unique
          cases unique with
          | cons _ _ _ _ tail => exact absent tail
        | isTrue tail =>
          apply isTrue
          apply CaptureKeysUnique.cons name key rest
          · intro otherName otherKey member
            exact present (otherName, otherKey) member
          · exact tail

instance (captures : List (String × String)) : Decidable (CaptureKeysUnique captures) :=
  decideCaptureKeysUnique captures

/- The values written back must have exactly the lexical names in the capture
   environment, in the same order. -/
inductive CaptureValuesMatch : List (String × String) → List (String × Nat) → Prop where
  | nil : CaptureValuesMatch [] []
  | cons (name key : String) (value : Nat) {captures values} :
      (name != name) = false → CaptureValuesMatch captures values →
      CaptureValuesMatch ((name, key) :: captures) ((name, value) :: values)

/- A successful raw writeback witnesses the frame's lexical shape. -/
theorem successful_store_captures_matches (captures : List (String × String)) (values : List (String × Nat))
    (store updated : List (String × Nat)) (written : storeCaptures store captures values = some updated) :
    CaptureValuesMatch captures values := by
  induction captures generalizing values store updated with
  | nil =>
    cases values with
    | nil => exact .nil
    | cons head tail => cases written
  | cons capture captures ih =>
    cases capture with
    | mk name key =>
      cases values with
      | nil => cases written
      | cons entry values =>
        cases entry with
        | mk actual value =>
          change (if name != actual then none else do
            let next ← updateBinding store key value
            storeCaptures next captures values) = some updated at written
          cases different : name != actual with
          | true => rw [different] at written; cases written
          | false =>
            have same : (name == actual) = true := by
              cases eq : name == actual with
              | true => rfl
              | false =>
                have impossible : (name != actual) = true := by
                  change (!(name == actual)) = true
                  rw [eq]
                  rfl
                rw [different] at impossible
                cases impossible
            have namesEqual : name = actual := beq_iff_eq.mp same
            cases namesEqual
            rw [different] at written
            change (updateBinding store key value).bind (fun next => storeCaptures next captures values) = some updated at written
            cases nextStore : updateBinding store key value with
            | none => rw [nextStore] at written; cases written
            | some next =>
              rw [nextStore] at written
              exact .cons name key value different (ih values next updated written)

theorem load_captures_update_other (captures : List (String × String)) (store updated : List (String × Nat))
    (key : String) (value : Nat)
    (different : ∀ name other, (name, other) ∈ captures → key ≠ other)
    (updatedStore : updateBinding store key value = some updated) :
    loadCaptures updated captures = loadCaptures store captures := by
  induction captures with
  | nil => rfl
  | cons entry rest ih =>
    cases entry with
    | mk name other =>
      have headDifferent := different name other (List.Mem.head rest)
      have tailDifferent : ∀ tailName tailKey, (tailName, tailKey) ∈ rest → key ≠ tailKey := by
        intro tailName tailKey member
        exact different tailName tailKey (List.Mem.tail _ member)
      have headEqual := lookup_update_other store updated key other value headDifferent updatedStore
      have tailEqual := ih tailDifferent
      change (do
        let head ← lookupBinding updated other
        let tail ← loadCaptures updated rest
        pure ((name, head) :: tail)) = (do
        let head ← lookupBinding store other
        let tail ← loadCaptures store rest
        pure ((name, head) :: tail))
      rw [headEqual, tailEqual]

/- With distinct cells and matching lexical names, any successfully loaded
   capture frame can be written back and read back exactly. -/
theorem store_captures_roundtrip (captures : List (String × String)) (values original : List (String × Nat))
    (store : List (String × Nat)) (unique : CaptureKeysUnique captures)
    (matching : CaptureValuesMatch captures values) (loaded : loadCaptures store captures = some original) :
    ∃ updated, storeCaptures store captures values = some updated ∧ loadCaptures updated captures = some values := by
  induction matching generalizing store original with
  | nil =>
    exact ⟨store, rfl, rfl⟩
  | @cons name key value captures values sameName matching ih =>
    cases unique with
    | cons _ _ _ fresh tailUnique =>
      change (lookupBinding store key).bind (fun head =>
        (loadCaptures store captures).bind (fun tail => some ((name, head) :: tail))) = some original at loaded
      cases headFound : lookupBinding store key with
      | none => rw [headFound] at loaded; cases loaded
      | some old =>
        cases tailFound : loadCaptures store captures with
        | none => rw [headFound, tailFound] at loaded; cases loaded
        | some oldTail =>
          obtain ⟨intermediate, updatedHead, readHead⟩ := update_existing_cell store key old value headFound
          have tailUnchanged := load_captures_update_other captures store intermediate key value fresh updatedHead
          have loadedTail : loadCaptures intermediate captures = some oldTail := tailUnchanged.trans tailFound
          obtain ⟨final, updatedTail, readTail⟩ := ih oldTail intermediate tailUnique loadedTail
          have headUnchanged := lookup_store_other captures values intermediate final key
            (by intro n k member; exact (fresh n k member).symm) updatedTail
          refine ⟨final, ?_, ?_⟩
          · change (if name != name then none else do
              let next ← updateBinding store key value
              storeCaptures next captures values) = some final
            rw [sameName, updatedHead]
            change storeCaptures intermediate captures values = some final
            exact updatedTail
          · change (do
              let head ← lookupBinding final key
              let tail ← loadCaptures final captures
              some ((name, head) :: tail)) = some ((name, value) :: values)
            rw [headUnchanged, readHead, readTail]
            rfl

/- Writeback returns a store only when reading its captured cells again yields
   exactly the updated lexical frame. This rejects conflicting aliases instead
   of silently losing a write. -/
def storeCapturedFrame (store : List (String × Nat)) (captures : List (String × String))
    (values : List (String × Nat)) : Option { result : List (String × Nat) // loadCaptures result captures = some values } := do
  let result ← storeCaptures store captures values
  if valid : loadCaptures result captures = some values then some ⟨result, valid⟩ else none

/- The machine's checked writeback cannot reject a well-shaped frame when all
   capture keys are distinct and the starting frame is present. -/
theorem distinct_captured_writeback_exists (captures : List (String × String)) (values original : List (String × Nat))
    (store : List (String × Nat)) (unique : CaptureKeysUnique captures)
    (matching : CaptureValuesMatch captures values) (loaded : loadCaptures store captures = some original) :
    ∃ result, storeCapturedFrame store captures values = some result := by
  obtain ⟨updated, written, readback⟩ := store_captures_roundtrip captures values original store unique matching loaded
  refine ⟨⟨updated, readback⟩, ?_⟩
  unfold storeCapturedFrame
  rw [written]
  change (if valid : loadCaptures updated captures = some values then
    some (⟨updated, valid⟩ : { result : List (String × Nat) // loadCaptures result captures = some values })
    else none) = some ⟨updated, readback⟩
  split
  · rfl
  · contradiction

/- Distinct corresponding environments can both write the same well-shaped
   lexical frame, even when their private allocation keys differ. -/
theorem distinct_related_writebacks_exist
    (leftStore rightStore : List (String × Nat)) (leftCapture rightCapture : List (String × String))
    (values : List (String × Nat))
    (related : relatedCapturedEnvironments leftStore rightStore leftCapture rightCapture)
    (leftUnique : CaptureKeysUnique leftCapture) (rightUnique : CaptureKeysUnique rightCapture)
    (leftMatching : CaptureValuesMatch leftCapture values) (rightMatching : CaptureValuesMatch rightCapture values) :
    (storeCapturedFrame leftStore leftCapture values).isSome = true ∧
      (storeCapturedFrame rightStore rightCapture values).isSome = true := by
  obtain ⟨original, leftLoaded, rightLoaded⟩ := related
  obtain ⟨leftResult, leftWritten⟩ :=
    distinct_captured_writeback_exists leftCapture values original leftStore leftUnique leftMatching leftLoaded
  obtain ⟨rightResult, rightWritten⟩ :=
    distinct_captured_writeback_exists rightCapture values original rightStore rightUnique rightMatching rightLoaded
  rw [leftWritten, rightWritten]
  exact ⟨rfl, rfl⟩

theorem storeCapturedFrame_origin (store : List (String × Nat)) (captures : List (String × String))
    (values : List (String × Nat))
    (result : { result : List (String × Nat) // loadCaptures result captures = some values })
    (written : storeCapturedFrame store captures values = some result) :
    storeCaptures store captures values = some result.val := by
  unfold storeCapturedFrame at written
  cases stored : storeCaptures store captures values with
  | none =>
    rw [stored] at written
    cases written
  | some next =>
    rw [stored] at written
    change (if valid : loadCaptures next captures = some values then some ⟨next, valid⟩ else none) = some result at written
    split at written
    · cases written
      rfl
    · cases written

/- Loading the same frame fixes the ordered lexical names, regardless of the
   private allocation keys. A writeback shape on one side therefore applies to
   the other side as well. -/
theorem capture_matching_transfer (leftCapture : List (String × String)) (values : List (String × Nat))
    (matching : CaptureValuesMatch leftCapture values) :
    ∀ (rightCapture : List (String × String)) (leftStore rightStore : List (String × Nat))
      (original : List (String × Nat)),
      loadCaptures leftStore leftCapture = some original →
      loadCaptures rightStore rightCapture = some original → CaptureValuesMatch rightCapture values := by
  induction matching with
  | nil =>
    intro rightCapture leftStore rightStore original leftLoaded rightLoaded
    change some [] = some original at leftLoaded
    cases leftLoaded
    cases rightCapture with
    | nil => exact .nil
    | cons head tail =>
      cases head with
      | mk name key =>
        change (do let value ← lookupBinding rightStore key
                   let rest ← loadCaptures rightStore tail
                   pure ((name, value) :: rest)) = some [] at rightLoaded
        cases h : lookupBinding rightStore key with
        | none => rw [h] at rightLoaded; cases rightLoaded
        | some v =>
          rw [h] at rightLoaded
          cases t : loadCaptures rightStore tail with
          | none => rw [t] at rightLoaded; cases rightLoaded
          | some vs => rw [t] at rightLoaded; cases rightLoaded
  | cons name key value sameName tail ih =>
    intro rightCapture leftStore rightStore original leftLoaded rightLoaded
    cases rightCapture with
    | nil =>
      change some [] = some original at rightLoaded
      cases rightLoaded
      change (do let v ← lookupBinding leftStore key
                 let rest ← loadCaptures leftStore _
                 pure ((name, v) :: rest)) = some [] at leftLoaded
      cases h : lookupBinding leftStore key with
      | none => rw [h] at leftLoaded; cases leftLoaded
      | some v =>
        rw [h] at leftLoaded
        cases t : loadCaptures leftStore _ with
        | none => rw [t] at leftLoaded; cases leftLoaded
        | some vs => rw [t] at leftLoaded; cases leftLoaded
    | cons head rest =>
      cases head with
      | mk other otherKey =>
        change (do let v ← lookupBinding leftStore key
                   let rest ← loadCaptures leftStore _
                   pure ((name, v) :: rest)) = some original at leftLoaded
        change (do let v ← lookupBinding rightStore otherKey
                   let tail ← loadCaptures rightStore rest
                   pure ((other, v) :: tail)) = some original at rightLoaded
        cases lv : lookupBinding leftStore key with
        | none => rw [lv] at leftLoaded; cases leftLoaded
        | some lvalue =>
          rw [lv] at leftLoaded
          cases lt : loadCaptures leftStore _ with
          | none => rw [lt] at leftLoaded; cases leftLoaded
          | some ltail =>
            rw [lt] at leftLoaded
            cases leftLoaded
            cases rv : lookupBinding rightStore otherKey with
            | none => rw [rv] at rightLoaded; cases rightLoaded
            | some rvalue =>
              rw [rv] at rightLoaded
              cases rt : loadCaptures rightStore rest with
              | none => rw [rt] at rightLoaded; cases rightLoaded
              | some rtail =>
                rw [rt] at rightLoaded
                cases rightLoaded
                exact .cons name otherKey value sameName (ih rest leftStore rightStore ltail lt rt)

theorem captured_frame_writeback_preserved
    (leftCapture rightCapture : List (String × String)) (values : List (String × Nat))
    (left : { result : List (String × Nat) // loadCaptures result leftCapture = some values })
    (right : { result : List (String × Nat) // loadCaptures result rightCapture = some values }) :
    relatedCapturedEnvironments left.val right.val leftCapture rightCapture :=
  ⟨values, left.property, right.property⟩

/- One call preserves corresponding capture frames when both writebacks
   succeed. The returned subtype is produced by the same checked writeback
   operation used by the machine; allocation keys need not be equal. -/
theorem captured_call_frame_preserved
    (globals leftStore rightStore : List (String × Nat)) (leftCapture rightCapture : List (String × String))
    (parameters : List String) (arguments : List Nat) (body : NumericExpr) (result : ExpressionResult Nat)
    (related : relatedCapturedEnvironments leftStore rightStore leftCapture rightCapture)
    (evaluated : evaluateCapturedFrame globals leftStore leftCapture parameters arguments body = some result)
    (left : { store : List (String × Nat) // loadCaptures store leftCapture = some (result.locals.drop parameters.length) })
    (right : { store : List (String × Nat) // loadCaptures store rightCapture = some (result.locals.drop parameters.length) }) :
    evaluateCapturedFrame globals rightStore rightCapture parameters arguments body = some result ∧
      relatedCapturedEnvironments left.val right.val leftCapture rightCapture := by
  constructor
  · rw [← captured_frame_evaluation_preserved globals leftStore rightStore leftCapture rightCapture parameters arguments body related]
    exact evaluated
  · exact captured_frame_writeback_preserved leftCapture rightCapture _ left right

def ExecutionState.cell (state : ExecutionState α) : Option Nat := lookupBinding state.bindings "$live"
def ExecutionState.snapshot (state : ExecutionState α) : Option Nat := lookupBinding state.bindings "$snapshot"

def recordObservation {α : Type} (state : ExecutionState α) (event : Observation) : ExecutionState α :=
  match state.replies with
  | [] => { state with events := state.events ++ [event] }
  | reply :: rest => { state with events := state.events ++ [event], replies := rest, error := reply }

def declare {α : Type} (state : ExecutionState α) (name : String) (value : Nat) : Option (ExecutionState α) :=
  if (lookupBinding state.bindings name).isSome || (lookupBinding state.captured name).isSome then none
  else some { state with bindings := state.bindings ++ [(name, value)] }

def declareCapture {α : Type} (state : ExecutionState α) (key : String) (value : Nat) : Option (ExecutionState α) :=
  if (lookupBinding state.captured key).isSome || (lookupBinding state.bindings key).isSome then none
  else some { state with captured := state.captured ++ [(key, value)] }

def incrementBy {α : Type} (state : ExecutionState α) (target : String) (value : Nat) : Option (ExecutionState α) := do
  let old ← lookupBinding state.bindings target
  let bindings ← updateBinding state.bindings target (old + value)
  pure { state with bindings }

def finishCapturedCall {α : Type} (state : ExecutionState α) (captured : List (String × Nat))
    (completion : Except ThrownValue Nat) : ExecutionState α :=
  match completion with
  | .error value => { state with captured, thrown := some value }
  | .ok value => { state with captured, returns := state.returns ++ [some value] }

/- Once both executions have written the same resulting frame, the actual
   machine transition preserves observations and the capture relation. -/
theorem captured_completion_preserved {α : Type}
    (leftState rightState : ExecutionState α) (leftCapture rightCapture : List (String × String))
    (values : List (String × Nat)) (completion : Except ThrownValue Nat)
    (left : { store : List (String × Nat) // loadCaptures store leftCapture = some values })
    (right : { store : List (String × Nat) // loadCaptures store rightCapture = some values })
    (sameBindings : leftState.bindings = rightState.bindings)
    (sameEvents : leftState.events = rightState.events)
    (sameReplies : leftState.replies = rightState.replies)
    (sameError : leftState.error = rightState.error)
    (sameReturns : leftState.returns = rightState.returns)
    (sameThrown : leftState.thrown = rightState.thrown) :
    let leftNext := finishCapturedCall leftState left.val completion
    let rightNext := finishCapturedCall rightState right.val completion
    leftNext.bindings = rightNext.bindings ∧ leftNext.events = rightNext.events ∧
      leftNext.replies = rightNext.replies ∧ leftNext.error = rightNext.error ∧
      leftNext.returns = rightNext.returns ∧ leftNext.thrown = rightNext.thrown ∧
      relatedCapturedEnvironments leftNext.captured rightNext.captured leftCapture rightCapture := by
  cases completion with
  | error value =>
    exact ⟨sameBindings, sameEvents, sameReplies, sameError, sameReturns, rfl,
      captured_frame_writeback_preserved leftCapture rightCapture values left right⟩
  | ok value =>
    exact ⟨sameBindings, sameEvents, sameReplies, sameError,
      congrArg (fun returns => returns ++ [some value]) sameReturns, sameThrown,
      captured_frame_writeback_preserved leftCapture rightCapture values left right⟩

theorem captured_machine_step_preserved {α : Type}
    (leftState rightState : ExecutionState α) (leftCapture rightCapture : List (String × String))
    (parameters : List String) (arguments : List Nat) (body : NumericExpr) (result : ExpressionResult Nat)
    (sameBindings : leftState.bindings = rightState.bindings)
    (sameEvents : leftState.events = rightState.events)
    (sameReplies : leftState.replies = rightState.replies)
    (sameError : leftState.error = rightState.error)
    (sameReturns : leftState.returns = rightState.returns)
    (sameThrown : leftState.thrown = rightState.thrown)
    (related : relatedCapturedEnvironments leftState.captured rightState.captured leftCapture rightCapture)
    (evaluated : evaluateCapturedFrame leftState.bindings leftState.captured leftCapture parameters arguments body = some result)
    (left : { store : List (String × Nat) // loadCaptures store leftCapture = some (result.locals.drop parameters.length) })
    (right : { store : List (String × Nat) // loadCaptures store rightCapture = some (result.locals.drop parameters.length) }) :
    evaluateCapturedFrame rightState.bindings rightState.captured rightCapture parameters arguments body = some result ∧
      let leftNext := finishCapturedCall leftState left.val result.completion
      let rightNext := finishCapturedCall rightState right.val result.completion
      leftNext.bindings = rightNext.bindings ∧ leftNext.events = rightNext.events ∧
        leftNext.replies = rightNext.replies ∧ leftNext.error = rightNext.error ∧
        leftNext.returns = rightNext.returns ∧ leftNext.thrown = rightNext.thrown ∧
        relatedCapturedEnvironments leftNext.captured rightNext.captured leftCapture rightCapture := by
  constructor
  · rw [← sameBindings]
    rw [← captured_frame_evaluation_preserved leftState.bindings leftState.captured rightState.captured leftCapture rightCapture parameters arguments body related]
    exact evaluated
  · exact captured_completion_preserved leftState rightState leftCapture rightCapture _ result.completion left right
      sameBindings sameEvents sameReplies sameError sameReturns sameThrown

def executeStep {α : Type} (state : ExecutionState α) (operation : Operation) : Option (ExecutionState α) :=
  match state.error with
  | some _ => some state
  | none =>
    if state.thrown.isSome then some state else
    match operation with
    | .initialize value => declare state "$live" value
    | .increment => incrementBy state "$live" 1
    | .captureSnapshot => do
      let value ← state.cell
      declare state "$snapshot" value
    | .incrementSnapshot => incrementBy state "$snapshot" 1
    | .readCell => do
      let value ← state.cell
      pure { state with events := state.events ++ [.number value] }
    | .readSnapshot => do
      let value ← state.snapshot
      pure { state with events := state.events ++ [.number value] }
    | .recordText value => some (recordObservation state (.text value))
    | .recordCell => do
      let value ← state.cell
      pure (recordObservation state (.number value))
    | .declareBinding name value => declare state name value
    | .copyBinding target source => do
      let value ← lookupBinding state.bindings source
      declare state target value
    | .incrementBinding name => incrementBy state name 1
    | .addBinding target source => do
      let value ← lookupBinding state.bindings source
      incrementBy state target value
    | .observeBinding name => do
      let value ← lookupBinding state.bindings name
      pure (recordObservation state (.number value))
    | .readBinding name => do
      let _ ← lookupBinding state.bindings name
      pure state
    | .returnBinding name => do
      let value ← lookupBinding state.bindings name
      pure { state with returns := state.returns ++ [some value] }
    | .returnUndefined => some { state with returns := state.returns ++ [none] }
    | .throwNumber value => some { state with thrown := some (.number value) }
    | .returnExpression expression parameters => do
      let result ← evaluateExpression parameters state.bindings expression
      match result.completion with
      | .error value => pure { state with thrown := some value }
      | .ok value => pure { state with returns := state.returns ++ [some value] }
    | .initializeCapture key expression parameters prior => do
      let captures ← loadCaptures state.captured prior
      let result ← evaluateExpression (parameters ++ captures) state.bindings expression
      match result.completion with
      | .error _ => none
      | .ok value => declareCapture state key value
    | .invokeCaptured captures parameters arguments body => do
      let result ← evaluateCapturedFrame state.bindings state.captured captures parameters arguments body
      let stored ← storeCapturedFrame state.captured captures (result.locals.drop parameters.length)
      pure (finishCapturedCall state stored.val result.completion)

def runExecution {α : Type} : ExecutionState α → List Operation → Option (ExecutionState α)
  | state, [] => some state
  | state, operation :: operations => do
    let next ← executeStep state operation
    runExecution next operations

theorem executeCaptured_success {α : Type} (state : ExecutionState α)
    (captures : List (String × String)) (parameters : List String) (arguments : List Nat)
    (body : NumericExpr) (result : ExpressionResult Nat)
    (stored : { store : List (String × Nat) // loadCaptures store captures = some (result.locals.drop parameters.length) })
    (noError : state.error = none) (noThrow : state.thrown = none)
    (evaluated : evaluateCapturedFrame state.bindings state.captured captures parameters arguments body = some result)
    (written : storeCapturedFrame state.captured captures (result.locals.drop parameters.length) = some stored) :
    executeStep state (.invokeCaptured captures parameters arguments body) =
      some (finishCapturedCall state stored.val result.completion) := by
  unfold executeStep
  rw [noError, noThrow]
  change (do
    let frame ← evaluateCapturedFrame state.bindings state.captured captures parameters arguments body
    let heap ← storeCapturedFrame state.captured captures (frame.locals.drop parameters.length)
    pure (finishCapturedCall state heap.val frame.completion)) = _
  rw [evaluated]
  change (do
    let heap ← storeCapturedFrame state.captured captures (result.locals.drop parameters.length)
    pure (finishCapturedCall state heap.val result.completion)) = _
  rw [written]
  rfl

theorem executeCaptured_decompose {α : Type} (state next : ExecutionState α)
    (captures : List (String × String)) (parameters : List String) (arguments : List Nat) (body : NumericExpr)
    (noError : state.error = none) (noThrow : state.thrown = none)
    (stepped : executeStep state (.invokeCaptured captures parameters arguments body) = some next) :
    ∃ (result : ExpressionResult Nat)
      (stored : { store : List (String × Nat) // loadCaptures store captures = some (result.locals.drop parameters.length) }),
      evaluateCapturedFrame state.bindings state.captured captures parameters arguments body = some result ∧
      storeCapturedFrame state.captured captures (result.locals.drop parameters.length) = some stored ∧
      next = finishCapturedCall state stored.val result.completion := by
  unfold executeStep at stepped
  rw [noError, noThrow] at stepped
  change (do
    let frame ← evaluateCapturedFrame state.bindings state.captured captures parameters arguments body
    let heap ← storeCapturedFrame state.captured captures (frame.locals.drop parameters.length)
    pure (finishCapturedCall state heap.val frame.completion)) = some next at stepped
  cases evaluated : evaluateCapturedFrame state.bindings state.captured captures parameters arguments body with
  | none =>
    rw [evaluated] at stepped
    cases stepped
  | some result =>
    rw [evaluated] at stepped
    change (do
      let heap ← storeCapturedFrame state.captured captures (result.locals.drop parameters.length)
      pure (finishCapturedCall state heap.val result.completion)) = some next at stepped
    cases written : storeCapturedFrame state.captured captures (result.locals.drop parameters.length) with
    | none =>
      rw [written] at stepped
      cases stepped
    | some stored =>
      rw [written] at stepped
      change some (finishCapturedCall state stored.val result.completion) = some next at stepped
      cases stepped
      exact ⟨result, stored, rfl, written, rfl⟩

theorem captured_step_preserves_disjoint {α : Type}
    (state next : ExecutionState α) (captures watched : List (String × String))
    (parameters : List String) (arguments : List Nat) (body : NumericExpr)
    (different : ∀ name key, (name, key) ∈ captures →
      ∀ watchedName watchedKey, (watchedName, watchedKey) ∈ watched → key ≠ watchedKey)
    (noError : state.error = none) (noThrow : state.thrown = none)
    (stepped : executeStep state (.invokeCaptured captures parameters arguments body) = some next) :
    loadCaptures next.captured watched = loadCaptures state.captured watched := by
  obtain ⟨result, stored, evaluated, written, finished⟩ :=
    executeCaptured_decompose state next captures parameters arguments body noError noThrow stepped
  have origin := storeCapturedFrame_origin state.captured captures (result.locals.drop parameters.length) stored written
  have unchanged := load_store_disjoint captures watched (result.locals.drop parameters.length)
    state.captured stored.val different origin
  rw [finished]
  cases result.completion with
  | error value => exact unchanged
  | ok value => exact unchanged

def capturesDisjoint (left right : List (String × String)) : Prop :=
  ∀ name key, (name, key) ∈ left → ∀ otherName otherKey, (otherName, otherKey) ∈ right → key ≠ otherKey

def captureKeyAbsent (key : String) (captures : List (String × String)) : Prop :=
  ∀ name other, (name, other) ∈ captures → key ≠ other

def decideCaptureKeyAbsent (key : String) (captures : List (String × String)) :
    Decidable (captureKeyAbsent key captures) :=
  match captures with
  | [] => isTrue (by intro name other member; cases member)
  | (name, other) :: rest =>
    if equal : key = other then
      isFalse (by
        intro absent
        exact absent name other (List.Mem.head rest) equal)
    else
      match decideCaptureKeyAbsent key rest with
      | isTrue tailAbsent => isTrue (by
          intro candidate candidateKey member
          cases member with
          | head => exact equal
          | tail _ tailMember => exact tailAbsent candidate candidateKey tailMember)
      | isFalse tailPresent => isFalse (by
          intro absent
          apply tailPresent
          intro candidate candidateKey tailMember
          exact absent candidate candidateKey (List.Mem.tail _ tailMember))

def decideCapturesDisjoint (left right : List (String × String)) : Decidable (capturesDisjoint left right) :=
  match left with
  | [] => isTrue (by intro name key member; cases member)
  | (name, key) :: rest =>
    match decideCaptureKeyAbsent key right, decideCapturesDisjoint rest right with
    | isFalse present, _ => isFalse (by
        intro disjoint
        apply present
        intro otherName otherKey otherMember
        exact disjoint name key (List.Mem.head rest) otherName otherKey otherMember)
    | isTrue absent, isFalse tailOverlaps => isFalse (by
        intro disjoint
        apply tailOverlaps
        intro candidate candidateKey tailMember otherName otherKey otherMember
        exact disjoint candidate candidateKey (List.Mem.tail _ tailMember) otherName otherKey otherMember)
    | isTrue absent, isTrue tailDisjoint => isTrue (by
        intro candidate candidateKey member otherName otherKey otherMember
        cases member with
        | head => exact absent otherName otherKey otherMember
        | tail _ tailMember => exact tailDisjoint candidate candidateKey tailMember otherName otherKey otherMember)

instance (left right : List (String × String)) : Decidable (capturesDisjoint left right) :=
  decideCapturesDisjoint left right

theorem captures_disjoint_symmetric (left right : List (String × String))
    (disjoint : capturesDisjoint left right) : capturesDisjoint right left := by
  intro name key member otherName otherKey otherMember
  exact (disjoint otherName otherKey otherMember name key member).symm

theorem captured_step_preserves_watched {α : Type}
    (state next : ExecutionState α) (captures watched : List (String × String))
    (parameters : List String) (arguments : List Nat) (body : NumericExpr)
    (disjoint : capturesDisjoint captures watched)
    (stepped : executeStep state (.invokeCaptured captures parameters arguments body) = some next) :
    loadCaptures next.captured watched = loadCaptures state.captured watched := by
  cases error : state.error with
  | some value =>
    have blocked : executeStep state (.invokeCaptured captures parameters arguments body) = some state := by
      unfold executeStep
      rw [error]
    rw [blocked] at stepped
    cases stepped
    rfl
  | none =>
    cases thrown : state.thrown with
    | some value =>
      have blocked : executeStep state (.invokeCaptured captures parameters arguments body) = some state := by
        unfold executeStep
        rw [error, thrown]
        rfl
      rw [blocked] at stepped
      cases stepped
      rfl
    | none =>
      exact captured_step_preserves_disjoint state next captures watched parameters arguments body disjoint error thrown stepped

theorem captured_paired_step_preserved {α : Type}
    (leftState rightState : ExecutionState α) (leftCapture rightCapture : List (String × String))
    (parameters : List String) (arguments : List Nat) (body : NumericExpr) (result : ExpressionResult Nat)
    (left : { store : List (String × Nat) // loadCaptures store leftCapture = some (result.locals.drop parameters.length) })
    (right : { store : List (String × Nat) // loadCaptures store rightCapture = some (result.locals.drop parameters.length) })
    (sameBindings : leftState.bindings = rightState.bindings)
    (sameEvents : leftState.events = rightState.events)
    (sameReplies : leftState.replies = rightState.replies)
    (sameError : leftState.error = rightState.error)
    (sameReturns : leftState.returns = rightState.returns)
    (sameThrown : leftState.thrown = rightState.thrown)
    (related : relatedCapturedEnvironments leftState.captured rightState.captured leftCapture rightCapture)
    (noError : leftState.error = none) (noThrow : leftState.thrown = none)
    (evaluated : evaluateCapturedFrame leftState.bindings leftState.captured leftCapture parameters arguments body = some result)
    (leftWritten : storeCapturedFrame leftState.captured leftCapture (result.locals.drop parameters.length) = some left)
    (rightWritten : storeCapturedFrame rightState.captured rightCapture (result.locals.drop parameters.length) = some right) :
    executeStep leftState (.invokeCaptured leftCapture parameters arguments body) =
        some (finishCapturedCall leftState left.val result.completion) ∧
      executeStep rightState (.invokeCaptured rightCapture parameters arguments body) =
        some (finishCapturedCall rightState right.val result.completion) ∧
      let leftNext := finishCapturedCall leftState left.val result.completion
      let rightNext := finishCapturedCall rightState right.val result.completion
      leftNext.bindings = rightNext.bindings ∧ leftNext.events = rightNext.events ∧
        leftNext.replies = rightNext.replies ∧ leftNext.error = rightNext.error ∧
        leftNext.returns = rightNext.returns ∧ leftNext.thrown = rightNext.thrown ∧
        relatedCapturedEnvironments leftNext.captured rightNext.captured leftCapture rightCapture := by
  have step := captured_machine_step_preserved leftState rightState leftCapture rightCapture parameters arguments body result
    sameBindings sameEvents sameReplies sameError sameReturns sameThrown related evaluated left right
  refine ⟨executeCaptured_success leftState leftCapture parameters arguments body result left noError noThrow evaluated leftWritten, ?_, step.2⟩
  exact executeCaptured_success rightState rightCapture parameters arguments body result right
    (sameError.symm.trans noError) (sameThrown.symm.trans noThrow) step.1 rightWritten

def capturedStatesRelated {α : Type} (leftCapture rightCapture : List (String × String))
    (left right : ExecutionState α) : Prop :=
  left.bindings = right.bindings ∧ left.events = right.events ∧ left.replies = right.replies ∧
    left.error = right.error ∧ left.returns = right.returns ∧ left.thrown = right.thrown ∧
    relatedCapturedEnvironments left.captured right.captured leftCapture rightCapture

instance {α : Type} [DecidableEq α] (leftCapture rightCapture : List (String × String))
    (left right : ExecutionState α) : Decidable (capturedStatesRelated leftCapture rightCapture left right) := by
  unfold capturedStatesRelated
  infer_instance

theorem captured_unblocked_step_related {α : Type}
    (leftCapture rightCapture : List (String × String)) (parameters : List String) (arguments : List Nat)
    (body : NumericExpr) (left right leftNext rightNext : ExecutionState α)
    (related : capturedStatesRelated leftCapture rightCapture left right)
    (noError : left.error = none) (noThrow : left.thrown = none)
    (leftStep : executeStep left (.invokeCaptured leftCapture parameters arguments body) = some leftNext)
    (rightStep : executeStep right (.invokeCaptured rightCapture parameters arguments body) = some rightNext) :
    capturedStatesRelated leftCapture rightCapture leftNext rightNext := by
  obtain ⟨sameBindings, sameEvents, sameReplies, sameError, sameReturns, sameThrown, sameCaptures⟩ := related
  obtain ⟨leftResult, leftStored, leftEvaluated, leftWritten, leftFinished⟩ :=
    executeCaptured_decompose left leftNext leftCapture parameters arguments body noError noThrow leftStep
  obtain ⟨rightResult, rightStored, rightEvaluated, rightWritten, rightFinished⟩ :=
    executeCaptured_decompose right rightNext rightCapture parameters arguments body
      (sameError.symm.trans noError) (sameThrown.symm.trans noThrow) rightStep
  have sameEvaluation : evaluateCapturedFrame left.bindings left.captured leftCapture parameters arguments body =
      evaluateCapturedFrame right.bindings right.captured rightCapture parameters arguments body := by
    rw [← sameBindings]
    exact captured_frame_evaluation_preserved left.bindings left.captured right.captured leftCapture rightCapture
      parameters arguments body sameCaptures
  have sameResult : leftResult = rightResult := Option.some.inj (leftEvaluated.symm.trans (sameEvaluation.trans rightEvaluated))
  cases sameResult
  rw [leftFinished, rightFinished]
  exact captured_completion_preserved left right leftCapture rightCapture _ leftResult.completion leftStored rightStored
    sameBindings sameEvents sameReplies sameError sameReturns sameThrown

theorem captured_step_related {α : Type}
    (leftCapture rightCapture : List (String × String)) (parameters : List String) (arguments : List Nat)
    (body : NumericExpr) (left right leftNext rightNext : ExecutionState α)
    (related : capturedStatesRelated leftCapture rightCapture left right)
    (leftStep : executeStep left (.invokeCaptured leftCapture parameters arguments body) = some leftNext)
    (rightStep : executeStep right (.invokeCaptured rightCapture parameters arguments body) = some rightNext) :
    capturedStatesRelated leftCapture rightCapture leftNext rightNext := by
  have sameError : left.error = right.error := related.2.2.2.1
  have sameThrown : left.thrown = right.thrown := related.2.2.2.2.2.1
  cases leftError : left.error with
  | some error =>
    have rightError : right.error = some error := sameError.symm.trans leftError
    have leftBlocked : executeStep left (.invokeCaptured leftCapture parameters arguments body) = some left := by
      unfold executeStep
      rw [leftError]
    have rightBlocked : executeStep right (.invokeCaptured rightCapture parameters arguments body) = some right := by
      unfold executeStep
      rw [rightError]
    rw [leftBlocked] at leftStep
    rw [rightBlocked] at rightStep
    cases leftStep
    cases rightStep
    exact related
  | none =>
    cases leftThrown : left.thrown with
    | some thrown =>
      have rightError : right.error = none := sameError.symm.trans leftError
      have rightThrown : right.thrown = some thrown := sameThrown.symm.trans leftThrown
      have leftBlocked : executeStep left (.invokeCaptured leftCapture parameters arguments body) = some left := by
        unfold executeStep
        rw [leftError, leftThrown]
        rfl
      have rightBlocked : executeStep right (.invokeCaptured rightCapture parameters arguments body) = some right := by
        unfold executeStep
        rw [rightError, rightThrown]
        rfl
      rw [leftBlocked] at leftStep
      rw [rightBlocked] at rightStep
      cases leftStep
      cases rightStep
      exact related
    | none =>
      exact captured_unblocked_step_related leftCapture rightCapture parameters arguments body left right leftNext rightNext
        related leftError leftThrown leftStep rightStep

/- A common loaded frame is insufficient when two lexical names alias the same
   cell on only one side: checked writeback can reject one store and accept the
   other. This condition isolates the additional allocation invariant needed
   for a total simulation. -/
def capturedWritebacksAgree (leftStore rightStore : List (String × Nat))
    (leftCapture rightCapture : List (String × String)) : Prop :=
  ∀ values, (storeCapturedFrame leftStore leftCapture values).isSome =
    (storeCapturedFrame rightStore rightCapture values).isSome

theorem distinct_writeback_transfer
    (leftStore rightStore : List (String × Nat)) (leftCapture rightCapture : List (String × String))
    (values : List (String × Nat))
    (related : relatedCapturedEnvironments leftStore rightStore leftCapture rightCapture)
    (rightUnique : CaptureKeysUnique rightCapture)
    (leftResult : { result : List (String × Nat) // loadCaptures result leftCapture = some values })
    (leftWritten : storeCapturedFrame leftStore leftCapture values = some leftResult) :
    ∃ rightResult, storeCapturedFrame rightStore rightCapture values = some rightResult := by
  obtain ⟨original, leftLoaded, rightLoaded⟩ := related
  have raw := storeCapturedFrame_origin leftStore leftCapture values leftResult leftWritten
  have matching := successful_store_captures_matches leftCapture values leftStore leftResult.val raw
  have rightMatching := capture_matching_transfer leftCapture values matching rightCapture leftStore rightStore original
    leftLoaded rightLoaded
  exact distinct_captured_writeback_exists rightCapture values original rightStore rightUnique rightMatching rightLoaded

theorem distinct_writebacks_agree
    (leftStore rightStore : List (String × Nat)) (leftCapture rightCapture : List (String × String))
    (related : relatedCapturedEnvironments leftStore rightStore leftCapture rightCapture)
    (leftUnique : CaptureKeysUnique leftCapture) (rightUnique : CaptureKeysUnique rightCapture) :
    capturedWritebacksAgree leftStore rightStore leftCapture rightCapture := by
  intro values
  cases leftWritten : storeCapturedFrame leftStore leftCapture values with
  | none =>
    cases rightWritten : storeCapturedFrame rightStore rightCapture values with
    | none => rfl
    | some rightResult =>
      obtain ⟨original, leftLoaded, rightLoaded⟩ := related
      obtain ⟨result, written⟩ := distinct_writeback_transfer rightStore leftStore rightCapture leftCapture values
        ⟨original, rightLoaded, leftLoaded⟩ leftUnique rightResult rightWritten
      rw [leftWritten] at written
      cases written
  | some leftResult =>
    obtain ⟨result, written⟩ := distinct_writeback_transfer leftStore rightStore leftCapture rightCapture values
      related rightUnique leftResult leftWritten
    rw [written]
    rfl

theorem captured_step_simulates {α : Type}
    (leftCapture rightCapture : List (String × String)) (parameters : List String) (arguments : List Nat)
    (body : NumericExpr) (left right leftNext : ExecutionState α)
    (related : capturedStatesRelated leftCapture rightCapture left right)
    (writebacks : capturedWritebacksAgree left.captured right.captured leftCapture rightCapture)
    (leftStep : executeStep left (.invokeCaptured leftCapture parameters arguments body) = some leftNext) :
    ∃ rightNext, executeStep right (.invokeCaptured rightCapture parameters arguments body) = some rightNext ∧
      capturedStatesRelated leftCapture rightCapture leftNext rightNext := by
  have sameError : left.error = right.error := related.2.2.2.1
  have sameThrown : left.thrown = right.thrown := related.2.2.2.2.2.1
  cases leftError : left.error with
  | some error =>
    have rightError : right.error = some error := sameError.symm.trans leftError
    have leftBlocked : executeStep left (.invokeCaptured leftCapture parameters arguments body) = some left := by
      unfold executeStep
      rw [leftError]
    have rightBlocked : executeStep right (.invokeCaptured rightCapture parameters arguments body) = some right := by
      unfold executeStep
      rw [rightError]
    rw [leftBlocked] at leftStep
    cases leftStep
    exact ⟨right, rightBlocked, related⟩
  | none =>
    cases leftThrown : left.thrown with
    | some thrown =>
      have rightError : right.error = none := sameError.symm.trans leftError
      have rightThrown : right.thrown = some thrown := sameThrown.symm.trans leftThrown
      have leftBlocked : executeStep left (.invokeCaptured leftCapture parameters arguments body) = some left := by
        unfold executeStep
        rw [leftError, leftThrown]
        rfl
      have rightBlocked : executeStep right (.invokeCaptured rightCapture parameters arguments body) = some right := by
        unfold executeStep
        rw [rightError, rightThrown]
        rfl
      rw [leftBlocked] at leftStep
      cases leftStep
      exact ⟨right, rightBlocked, related⟩
    | none =>
      obtain ⟨result, leftStored, evaluated, written, finished⟩ :=
        executeCaptured_decompose left leftNext leftCapture parameters arguments body leftError leftThrown leftStep
      have sameEvaluation : evaluateCapturedFrame left.bindings left.captured leftCapture parameters arguments body =
          evaluateCapturedFrame right.bindings right.captured rightCapture parameters arguments body := by
        rw [← related.1]
        exact captured_frame_evaluation_preserved left.bindings left.captured right.captured leftCapture rightCapture
          parameters arguments body related.2.2.2.2.2.2
      have rightEvaluated := sameEvaluation.symm.trans evaluated
      have writebackExists := writebacks (result.locals.drop parameters.length)
      rw [written] at writebackExists
      cases rightWritten : storeCapturedFrame right.captured rightCapture (result.locals.drop parameters.length) with
      | none => rw [rightWritten] at writebackExists; cases writebackExists
      | some rightStored =>
        have rightStep := executeCaptured_success right rightCapture parameters arguments body result rightStored
          (sameError.symm.trans leftError) (sameThrown.symm.trans leftThrown) rightEvaluated rightWritten
        exact ⟨_, rightStep, captured_step_related leftCapture rightCapture parameters arguments body left right leftNext _
          related leftStep rightStep⟩

inductive RelatedOperations (relation : Operation → Operation → Prop) : List Operation → List Operation → Prop where
  | nil : RelatedOperations relation [] []
  | cons {left right leftTail rightTail} : relation left right → RelatedOperations relation leftTail rightTail →
      RelatedOperations relation (left :: leftTail) (right :: rightTail)

/- Lift a relation preserved by each pair of machine steps to any finite pair
   of operation sequences. Both executions must complete in the model. -/
theorem runExecution_relation_preserved {α : Type}
    (stateRel : ExecutionState α → ExecutionState α → Prop)
    (operationRel : Operation → Operation → Prop)
    (preserve : ∀ left right leftOp rightOp leftNext rightNext,
      stateRel left right → operationRel leftOp rightOp →
      executeStep left leftOp = some leftNext → executeStep right rightOp = some rightNext →
      stateRel leftNext rightNext)
    (leftOps rightOps : List Operation) (operations : RelatedOperations operationRel leftOps rightOps)
    (left right leftFinal rightFinal : ExecutionState α)
    (initial : stateRel left right)
    (leftRun : runExecution left leftOps = some leftFinal)
    (rightRun : runExecution right rightOps = some rightFinal) : stateRel leftFinal rightFinal := by
  induction operations generalizing left right with
  | nil =>
    change some left = some leftFinal at leftRun
    change some right = some rightFinal at rightRun
    cases leftRun
    cases rightRun
    exact initial
  | @cons leftOp rightOp leftTail rightTail opRelated tailsRelated inductionHypothesis =>
    change (executeStep left leftOp).bind (fun next => runExecution next leftTail) = some leftFinal at leftRun
    change (executeStep right rightOp).bind (fun next => runExecution next rightTail) = some rightFinal at rightRun
    cases leftStep : executeStep left leftOp with
    | none =>
      rw [leftStep] at leftRun
      cases leftRun
    | some leftNext =>
      cases rightStep : executeStep right rightOp with
      | none =>
        rw [rightStep] at rightRun
        cases rightRun
      | some rightNext =>
        rw [leftStep] at leftRun
        rw [rightStep] at rightRun
        exact inductionHypothesis leftNext rightNext
          (preserve left right leftOp rightOp leftNext rightNext initial opRelated leftStep rightStep)
          leftRun rightRun

/- Unlike the two-completed-runs lemma, this constructs the matching run from
   a one-step simulation. The premise must cover every reachable related state. -/
theorem runExecution_simulates {α : Type}
    (stateRel : ExecutionState α → ExecutionState α → Prop)
    (operationRel : Operation → Operation → Prop)
    (simulate : ∀ left right leftOp rightOp leftNext,
      stateRel left right → operationRel leftOp rightOp → executeStep left leftOp = some leftNext →
      ∃ rightNext, executeStep right rightOp = some rightNext ∧ stateRel leftNext rightNext)
    (leftOps rightOps : List Operation) (operations : RelatedOperations operationRel leftOps rightOps)
    (left right leftFinal : ExecutionState α) (initial : stateRel left right)
    (leftRun : runExecution left leftOps = some leftFinal) :
    ∃ rightFinal, runExecution right rightOps = some rightFinal ∧ stateRel leftFinal rightFinal := by
  induction operations generalizing left right with
  | nil =>
    change some left = some leftFinal at leftRun
    cases leftRun
    exact ⟨right, rfl, initial⟩
  | @cons leftOp rightOp leftTail rightTail opRelated tailsRelated inductionHypothesis =>
    change (executeStep left leftOp).bind (fun next => runExecution next leftTail) = some leftFinal at leftRun
    cases leftStep : executeStep left leftOp with
    | none => rw [leftStep] at leftRun; cases leftRun
    | some leftNext =>
      rw [leftStep] at leftRun
      obtain ⟨rightNext, rightStep, nextRelated⟩ := simulate left right leftOp rightOp leftNext initial opRelated leftStep
      obtain ⟨rightFinal, rightRun, finalRelated⟩ := inductionHypothesis leftNext rightNext nextRelated leftRun
      refine ⟨rightFinal, ?_, finalRelated⟩
      change (executeStep right rightOp).bind (fun next => runExecution next rightTail) = some rightFinal
      rw [rightStep]
      exact rightRun

inductive PairedCapturedOperation (leftCapture rightCapture : List (String × String)) : Operation → Operation → Prop where
  | call (parameters : List String) (arguments : List Nat) (body : NumericExpr) :
      PairedCapturedOperation leftCapture rightCapture (.invokeCaptured leftCapture parameters arguments body)
        (.invokeCaptured rightCapture parameters arguments body)

theorem captured_call_sequence_related {α : Type}
    (leftCapture rightCapture : List (String × String)) (leftOps rightOps : List Operation)
    (operations : RelatedOperations (PairedCapturedOperation leftCapture rightCapture) leftOps rightOps)
    (left right leftFinal rightFinal : ExecutionState α)
    (initial : capturedStatesRelated leftCapture rightCapture left right)
    (leftRun : runExecution left leftOps = some leftFinal)
    (rightRun : runExecution right rightOps = some rightFinal) :
    capturedStatesRelated leftCapture rightCapture leftFinal rightFinal := by
  apply runExecution_relation_preserved (capturedStatesRelated leftCapture rightCapture)
    (PairedCapturedOperation leftCapture rightCapture) ?_ leftOps rightOps operations left right leftFinal rightFinal
    initial leftRun rightRun
  intro leftState rightState leftOp rightOp leftStateNext rightStateNext related operation leftStep rightStep
  cases operation with
  | call parameters arguments body =>
    exact captured_step_related leftCapture rightCapture parameters arguments body leftState rightState leftStateNext
      rightStateNext related leftStep rightStep

theorem captured_step_preserves_pair {α : Type}
    (leftCapture rightCapture leftWatched rightWatched : List (String × String))
    (parameters : List String) (arguments : List Nat) (body : NumericExpr)
    (left right leftNext rightNext : ExecutionState α)
    (active : capturedStatesRelated leftCapture rightCapture left right)
    (watched : relatedCapturedEnvironments left.captured right.captured leftWatched rightWatched)
    (leftDisjoint : capturesDisjoint leftCapture leftWatched)
    (rightDisjoint : capturesDisjoint rightCapture rightWatched)
    (leftStep : executeStep left (.invokeCaptured leftCapture parameters arguments body) = some leftNext)
    (rightStep : executeStep right (.invokeCaptured rightCapture parameters arguments body) = some rightNext) :
    capturedStatesRelated leftCapture rightCapture leftNext rightNext ∧
      relatedCapturedEnvironments leftNext.captured rightNext.captured leftWatched rightWatched := by
  constructor
  · exact captured_step_related leftCapture rightCapture parameters arguments body left right leftNext rightNext
      active leftStep rightStep
  · obtain ⟨frame, leftFrame, rightFrame⟩ := watched
    have leftUnchanged := captured_step_preserves_watched left leftNext leftCapture leftWatched
      parameters arguments body leftDisjoint leftStep
    have rightUnchanged := captured_step_preserves_watched right rightNext rightCapture rightWatched
      parameters arguments body rightDisjoint rightStep
    exact ⟨frame, leftUnchanged.trans leftFrame, rightUnchanged.trans rightFrame⟩

theorem capturedStates_related_with_environment {α : Type}
    (leftCapture rightCapture leftOther rightOther : List (String × String)) (left right : ExecutionState α)
    (related : capturedStatesRelated leftCapture rightCapture left right)
    (other : relatedCapturedEnvironments left.captured right.captured leftOther rightOther) :
    capturedStatesRelated leftOther rightOther left right := by
  obtain ⟨bindings, events, replies, error, returns, thrown, captures⟩ := related
  exact ⟨bindings, events, replies, error, returns, thrown, other⟩

def twoCapturedStatesRelated {α : Type} (leftFirst rightFirst leftSecond rightSecond : List (String × String))
    (left right : ExecutionState α) : Prop :=
  capturedStatesRelated leftFirst rightFirst left right ∧ capturedStatesRelated leftSecond rightSecond left right

instance {α : Type} [DecidableEq α] (leftFirst rightFirst leftSecond rightSecond : List (String × String))
    (left right : ExecutionState α) : Decidable (twoCapturedStatesRelated leftFirst rightFirst leftSecond rightSecond left right) := by
  unfold twoCapturedStatesRelated
  infer_instance

inductive PairedTwoCapturedOperation
    (leftFirst rightFirst leftSecond rightSecond : List (String × String)) : Operation → Operation → Prop where
  | first (parameters : List String) (arguments : List Nat) (body : NumericExpr) :
      PairedTwoCapturedOperation leftFirst rightFirst leftSecond rightSecond
        (.invokeCaptured leftFirst parameters arguments body) (.invokeCaptured rightFirst parameters arguments body)
  | second (parameters : List String) (arguments : List Nat) (body : NumericExpr) :
      PairedTwoCapturedOperation leftFirst rightFirst leftSecond rightSecond
        (.invokeCaptured leftSecond parameters arguments body) (.invokeCaptured rightSecond parameters arguments body)

theorem two_captured_call_sequence_related {α : Type}
    (leftFirst rightFirst leftSecond rightSecond : List (String × String))
    (leftDisjoint : capturesDisjoint leftFirst leftSecond) (rightDisjoint : capturesDisjoint rightFirst rightSecond)
    (leftOps rightOps : List Operation)
    (operations : RelatedOperations (PairedTwoCapturedOperation leftFirst rightFirst leftSecond rightSecond) leftOps rightOps)
    (left right leftFinal rightFinal : ExecutionState α)
    (initial : twoCapturedStatesRelated leftFirst rightFirst leftSecond rightSecond left right)
    (leftRun : runExecution left leftOps = some leftFinal) (rightRun : runExecution right rightOps = some rightFinal) :
    twoCapturedStatesRelated leftFirst rightFirst leftSecond rightSecond leftFinal rightFinal := by
  apply runExecution_relation_preserved (twoCapturedStatesRelated leftFirst rightFirst leftSecond rightSecond)
    (PairedTwoCapturedOperation leftFirst rightFirst leftSecond rightSecond) ?_ leftOps rightOps operations
    left right leftFinal rightFinal initial leftRun rightRun
  intro leftState rightState leftOp rightOp leftNext rightNext related operation leftStep rightStep
  cases operation with
  | first parameters arguments body =>
    have pair := captured_step_preserves_pair leftFirst rightFirst leftSecond rightSecond parameters arguments body
      leftState rightState leftNext rightNext related.1 related.2.2.2.2.2.2.2 leftDisjoint rightDisjoint leftStep rightStep
    exact ⟨pair.1, capturedStates_related_with_environment leftFirst rightFirst leftSecond rightSecond leftNext rightNext pair.1 pair.2⟩
  | second parameters arguments body =>
    have pair := captured_step_preserves_pair leftSecond rightSecond leftFirst rightFirst parameters arguments body
      leftState rightState leftNext rightNext related.2 related.1.2.2.2.2.2.2
      (captures_disjoint_symmetric leftFirst leftSecond leftDisjoint)
      (captures_disjoint_symmetric rightFirst rightSecond rightDisjoint) leftStep rightStep
    exact ⟨capturedStates_related_with_environment leftSecond rightSecond leftFirst rightFirst leftNext rightNext pair.1 pair.2, pair.1⟩

/- When the allocation invariant guarantees writeback agreement in every
   related state, an entire successful two-closure run has a matching run. -/
theorem two_captured_call_sequence_simulates {α : Type}
    (leftFirst rightFirst leftSecond rightSecond : List (String × String))
    (leftDisjoint : capturesDisjoint leftFirst leftSecond) (rightDisjoint : capturesDisjoint rightFirst rightSecond)
    (writebacks : ∀ left right : ExecutionState α,
      twoCapturedStatesRelated leftFirst rightFirst leftSecond rightSecond left right →
      capturedWritebacksAgree left.captured right.captured leftFirst rightFirst ∧
      capturedWritebacksAgree left.captured right.captured leftSecond rightSecond)
    (leftOps rightOps : List Operation)
    (operations : RelatedOperations (PairedTwoCapturedOperation leftFirst rightFirst leftSecond rightSecond) leftOps rightOps)
    (left right leftFinal : ExecutionState α)
    (initial : twoCapturedStatesRelated leftFirst rightFirst leftSecond rightSecond left right)
    (leftRun : runExecution left leftOps = some leftFinal) :
    ∃ rightFinal, runExecution right rightOps = some rightFinal ∧
      twoCapturedStatesRelated leftFirst rightFirst leftSecond rightSecond leftFinal rightFinal := by
  apply runExecution_simulates (twoCapturedStatesRelated leftFirst rightFirst leftSecond rightSecond)
    (PairedTwoCapturedOperation leftFirst rightFirst leftSecond rightSecond) ?_ leftOps rightOps operations
    left right leftFinal initial leftRun
  intro leftState rightState leftOp rightOp leftNext related operation leftStep
  cases operation with
  | first parameters arguments body =>
    obtain ⟨rightNext, rightStep, active⟩ := captured_step_simulates leftFirst rightFirst parameters arguments body
      leftState rightState leftNext related.1 (writebacks leftState rightState related).1 leftStep
    have pair := captured_step_preserves_pair leftFirst rightFirst leftSecond rightSecond parameters arguments body
      leftState rightState leftNext rightNext related.1 related.2.2.2.2.2.2.2 leftDisjoint rightDisjoint leftStep rightStep
    exact ⟨rightNext, rightStep, pair.1,
      capturedStates_related_with_environment leftFirst rightFirst leftSecond rightSecond leftNext rightNext pair.1 pair.2⟩
  | second parameters arguments body =>
    obtain ⟨rightNext, rightStep, active⟩ := captured_step_simulates leftSecond rightSecond parameters arguments body
      leftState rightState leftNext related.2 (writebacks leftState rightState related).2 leftStep
    have pair := captured_step_preserves_pair leftSecond rightSecond leftFirst rightFirst parameters arguments body
      leftState rightState leftNext rightNext related.2 related.1.2.2.2.2.2.2
      (captures_disjoint_symmetric leftFirst leftSecond leftDisjoint)
      (captures_disjoint_symmetric rightFirst rightSecond rightDisjoint) leftStep rightStep
    exact ⟨rightNext, rightStep,
      capturedStates_related_with_environment leftSecond rightSecond leftFirst rightFirst leftNext rightNext pair.1 pair.2, pair.1⟩

/- Distinct keys discharge the writeback premise for every reachable pair of
   states; clients need only establish the static allocation invariant. -/
theorem two_distinct_captured_call_sequence_simulates {α : Type}
    (leftFirst rightFirst leftSecond rightSecond : List (String × String))
    (leftDisjoint : capturesDisjoint leftFirst leftSecond) (rightDisjoint : capturesDisjoint rightFirst rightSecond)
    (leftFirstUnique : CaptureKeysUnique leftFirst) (rightFirstUnique : CaptureKeysUnique rightFirst)
    (leftSecondUnique : CaptureKeysUnique leftSecond) (rightSecondUnique : CaptureKeysUnique rightSecond)
    (leftOps rightOps : List Operation)
    (operations : RelatedOperations (PairedTwoCapturedOperation leftFirst rightFirst leftSecond rightSecond) leftOps rightOps)
    (left right leftFinal : ExecutionState α)
    (initial : twoCapturedStatesRelated leftFirst rightFirst leftSecond rightSecond left right)
    (leftRun : runExecution left leftOps = some leftFinal) :
    ∃ rightFinal, runExecution right rightOps = some rightFinal ∧
      twoCapturedStatesRelated leftFirst rightFirst leftSecond rightSecond leftFinal rightFinal := by
  apply two_captured_call_sequence_simulates leftFirst rightFirst leftSecond rightSecond leftDisjoint rightDisjoint
    ?_ leftOps rightOps operations left right leftFinal initial leftRun
  intro l r related
  exact ⟨distinct_writebacks_agree l.captured r.captured leftFirst rightFirst related.1.2.2.2.2.2.2
      leftFirstUnique rightFirstUnique,
    distinct_writebacks_agree l.captured r.captured leftSecond rightSecond related.2.2.2.2.2.2.2
      leftSecondUnique rightSecondUnique⟩

theorem captured_states_related_symmetric {α : Type} (leftCapture rightCapture : List (String × String))
    (left right : ExecutionState α) (related : capturedStatesRelated leftCapture rightCapture left right) :
    capturedStatesRelated rightCapture leftCapture right left := by
  obtain ⟨bindings, events, replies, error, returns, thrown, frame, leftLoaded, rightLoaded⟩ := related
  exact ⟨bindings.symm, events.symm, replies.symm, error.symm, returns.symm, thrown.symm,
    frame, rightLoaded, leftLoaded⟩

theorem paired_two_operations_symmetric
    (leftFirst rightFirst leftSecond rightSecond : List (String × String)) (leftOps rightOps : List Operation)
    (operations : RelatedOperations (PairedTwoCapturedOperation leftFirst rightFirst leftSecond rightSecond)
      leftOps rightOps) :
    RelatedOperations (PairedTwoCapturedOperation rightFirst leftFirst rightSecond leftSecond) rightOps leftOps := by
  induction operations with
  | nil => exact .nil
  | cons operation tail ih =>
    cases operation with
    | first parameters arguments body => exact .cons (.first parameters arguments body) ih
    | second parameters arguments body => exact .cons (.second parameters arguments body) ih

/- Successful completion on either side implies successful completion on the
   other, including the model's failure cases. -/
theorem two_distinct_captured_runs_complete_together {α : Type}
    (leftFirst rightFirst leftSecond rightSecond : List (String × String))
    (leftDisjoint : capturesDisjoint leftFirst leftSecond) (rightDisjoint : capturesDisjoint rightFirst rightSecond)
    (leftFirstUnique : CaptureKeysUnique leftFirst) (rightFirstUnique : CaptureKeysUnique rightFirst)
    (leftSecondUnique : CaptureKeysUnique leftSecond) (rightSecondUnique : CaptureKeysUnique rightSecond)
    (leftOps rightOps : List Operation)
    (operations : RelatedOperations (PairedTwoCapturedOperation leftFirst rightFirst leftSecond rightSecond) leftOps rightOps)
    (left right : ExecutionState α)
    (initial : twoCapturedStatesRelated leftFirst rightFirst leftSecond rightSecond left right) :
    (runExecution left leftOps).isSome = (runExecution right rightOps).isSome := by
  cases leftRun : runExecution left leftOps with
  | none =>
    cases rightRun : runExecution right rightOps with
    | none => rfl
    | some rightFinal =>
      have reversed : twoCapturedStatesRelated rightFirst leftFirst rightSecond leftSecond right left :=
        ⟨captured_states_related_symmetric leftFirst rightFirst left right initial.1,
         captured_states_related_symmetric leftSecond rightSecond left right initial.2⟩
      obtain ⟨leftFinal, resulting, _⟩ :=
        two_distinct_captured_call_sequence_simulates rightFirst leftFirst rightSecond leftSecond
          rightDisjoint leftDisjoint rightFirstUnique leftFirstUnique rightSecondUnique leftSecondUnique
          rightOps leftOps (paired_two_operations_symmetric leftFirst rightFirst leftSecond rightSecond leftOps rightOps operations)
          right left rightFinal reversed rightRun
      rw [leftRun] at resulting
      cases resulting
  | some leftFinal =>
    obtain ⟨rightFinal, resulting, _⟩ :=
      two_distinct_captured_call_sequence_simulates leftFirst rightFirst leftSecond rightSecond
        leftDisjoint rightDisjoint leftFirstUnique rightFirstUnique leftSecondUnique rightSecondUnique
        leftOps rightOps operations left right leftFinal initial leftRun
    rw [resulting]
    rfl

/- A finite collection of corresponding environments. Distinct entries must
   be disjoint, while repeated entries represent aliases of the same closure. -/
abbrev CapturePair := List (String × String) × List (String × String)

def capturePairsRelated {α : Type} (pairs : List CapturePair) (left right : ExecutionState α) : Prop :=
  ∀ pair ∈ pairs, capturedStatesRelated pair.1 pair.2 left right

def capturePairsUnique (pairs : List CapturePair) : Prop :=
  ∀ pair ∈ pairs, CaptureKeysUnique pair.1 ∧ CaptureKeysUnique pair.2

def capturePairsDisjoint (pairs : List CapturePair) : Prop :=
  ∀ pair ∈ pairs, ∀ other ∈ pairs, pair ≠ other →
    capturesDisjoint pair.1 other.1 ∧ capturesDisjoint pair.2 other.2

instance {α : Type} [DecidableEq α] (pairs : List CapturePair) (left right : ExecutionState α) :
    Decidable (capturePairsRelated pairs left right) := by
  unfold capturePairsRelated
  infer_instance

instance (pairs : List CapturePair) : Decidable (capturePairsUnique pairs) := by
  unfold capturePairsUnique
  infer_instance

instance (pairs : List CapturePair) : Decidable (capturePairsDisjoint pairs) := by
  unfold capturePairsDisjoint
  infer_instance

inductive PairedCapturedListOperation (pairs : List CapturePair) : Operation → Operation → Prop where
  | invoke (leftCapture rightCapture : List (String × String)) (parameters : List String)
      (arguments : List Nat) (body : NumericExpr) : (leftCapture, rightCapture) ∈ pairs →
      PairedCapturedListOperation pairs (.invokeCaptured leftCapture parameters arguments body)
        (.invokeCaptured rightCapture parameters arguments body)

theorem captured_pairs_step_simulates {α : Type} (pairs : List CapturePair)
    (unique : capturePairsUnique pairs) (disjoint : capturePairsDisjoint pairs)
    (left right leftNext : ExecutionState α) (leftOp rightOp : Operation)
    (related : capturePairsRelated pairs left right) (operation : PairedCapturedListOperation pairs leftOp rightOp)
    (leftStep : executeStep left leftOp = some leftNext) :
    ∃ rightNext, executeStep right rightOp = some rightNext ∧ capturePairsRelated pairs leftNext rightNext := by
  cases operation with
  | invoke leftCapture rightCapture parameters arguments body member =>
    have active := related (leftCapture, rightCapture) member
    have distinct := unique (leftCapture, rightCapture) member
    obtain ⟨rightNext, rightStep, nextActive⟩ := captured_step_simulates leftCapture rightCapture parameters arguments body
      left right leftNext active
      (distinct_writebacks_agree left.captured right.captured leftCapture rightCapture active.2.2.2.2.2.2
        distinct.1 distinct.2) leftStep
    refine ⟨rightNext, rightStep, ?_⟩
    intro other otherMember
    cases other with
    | mk leftOther rightOther =>
      by_cases same : (leftCapture, rightCapture) = (leftOther, rightOther)
      · cases same
        exact nextActive
      · have separate := disjoint (leftCapture, rightCapture) member (leftOther, rightOther) otherMember same
        have watched := (related (leftOther, rightOther) otherMember).2.2.2.2.2.2
        have preserved := captured_step_preserves_pair leftCapture rightCapture leftOther rightOther parameters arguments body
          left right leftNext rightNext active watched separate.1 separate.2 leftStep rightStep
        exact capturedStates_related_with_environment leftCapture rightCapture leftOther rightOther leftNext rightNext
          preserved.1 preserved.2

theorem captured_pairs_sequence_simulates {α : Type} (pairs : List CapturePair)
    (unique : capturePairsUnique pairs) (disjoint : capturePairsDisjoint pairs)
    (leftOps rightOps : List Operation) (operations : RelatedOperations (PairedCapturedListOperation pairs) leftOps rightOps)
    (left right leftFinal : ExecutionState α) (initial : capturePairsRelated pairs left right)
    (leftRun : runExecution left leftOps = some leftFinal) :
    ∃ rightFinal, runExecution right rightOps = some rightFinal ∧ capturePairsRelated pairs leftFinal rightFinal := by
  apply runExecution_simulates (capturePairsRelated pairs) (PairedCapturedListOperation pairs) ?_
    leftOps rightOps operations left right leftFinal initial leftRun
  intro l r lo ro next related operation step
  exact captured_pairs_step_simulates pairs unique disjoint l r next lo ro related operation step

def swapCapturePair (pair : CapturePair) : CapturePair := (pair.2, pair.1)

theorem map_member_forward {α β : Type} (f : α → β) (items : List α) (item : α)
    (member : item ∈ items) : f item ∈ items.map f := by
  induction member with
  | head => exact .head _
  | tail _ _ ih => exact .tail _ ih

theorem map_member_backward {α β : Type} (f : α → β) (items : List α) (value : β)
    (member : value ∈ items.map f) : ∃ item, item ∈ items ∧ f item = value := by
  induction items with
  | nil => cases member
  | cons head tail ih =>
    change value ∈ f head :: tail.map f at member
    cases member with
    | head => exact ⟨head, .head _, rfl⟩
    | tail _ rest =>
      obtain ⟨item, found, equal⟩ := ih rest
      exact ⟨item, .tail _ found, equal⟩

theorem swap_capture_pair_member (pairs : List CapturePair) (leftCapture rightCapture : List (String × String))
    (member : (leftCapture, rightCapture) ∈ pairs) :
    (rightCapture, leftCapture) ∈ pairs.map swapCapturePair :=
  map_member_forward swapCapturePair pairs (leftCapture, rightCapture) member

theorem swapped_capture_pairs_unique (pairs : List CapturePair) (unique : capturePairsUnique pairs) :
    capturePairsUnique (pairs.map swapCapturePair) := by
  intro target member
  obtain ⟨pair, original, equal⟩ := map_member_backward swapCapturePair pairs _ member
  rcases target with ⟨leftCapture, rightCapture⟩
  cases pair with
  | mk left right =>
    change (right, left) = (leftCapture, rightCapture) at equal
    cases equal
    have result := unique (rightCapture, leftCapture) original
    exact ⟨result.2, result.1⟩

theorem swapped_capture_pairs_disjoint (pairs : List CapturePair) (disjoint : capturePairsDisjoint pairs) :
    capturePairsDisjoint (pairs.map swapCapturePair) := by
  intro target member otherTarget otherMember different
  obtain ⟨pair, original, equal⟩ := map_member_backward swapCapturePair pairs _ member
  obtain ⟨other, originalOther, equalOther⟩ := map_member_backward swapCapturePair pairs _ otherMember
  rcases target with ⟨leftCapture, rightCapture⟩
  rcases otherTarget with ⟨leftOther, rightOther⟩
  cases pair with
  | mk left right =>
    cases other with
    | mk otherLeft otherRight =>
      change (right, left) = (leftCapture, rightCapture) at equal
      change (otherRight, otherLeft) = (leftOther, rightOther) at equalOther
      cases equal
      cases equalOther
      have notEqual : (rightCapture, leftCapture) ≠ (rightOther, leftOther) := by
        intro same
        cases same
        exact different rfl
      have result := disjoint (rightCapture, leftCapture) original (rightOther, leftOther) originalOther notEqual
      exact ⟨result.2, result.1⟩

theorem swapped_capture_pairs_related {α : Type} (pairs : List CapturePair) (left right : ExecutionState α)
    (related : capturePairsRelated pairs left right) : capturePairsRelated (pairs.map swapCapturePair) right left := by
  intro target member
  obtain ⟨pair, original, equal⟩ := map_member_backward swapCapturePair pairs _ member
  rcases target with ⟨leftCapture, rightCapture⟩
  cases pair with
  | mk l r =>
    change (r, l) = (leftCapture, rightCapture) at equal
    cases equal
    exact captured_states_related_symmetric rightCapture leftCapture left right (related (rightCapture, leftCapture) original)

theorem swapped_capture_pair_operations (pairs : List CapturePair) (leftOps rightOps : List Operation)
    (operations : RelatedOperations (PairedCapturedListOperation pairs) leftOps rightOps) :
    RelatedOperations (PairedCapturedListOperation (pairs.map swapCapturePair)) rightOps leftOps := by
  induction operations with
  | nil => exact .nil
  | cons operation tail ih =>
    cases operation with
    | invoke leftCapture rightCapture parameters arguments body member =>
      exact .cons (.invoke rightCapture leftCapture parameters arguments body
        (swap_capture_pair_member pairs leftCapture rightCapture member)) ih

theorem captured_pairs_runs_complete_together {α : Type} (pairs : List CapturePair)
    (unique : capturePairsUnique pairs) (disjoint : capturePairsDisjoint pairs)
    (leftOps rightOps : List Operation) (operations : RelatedOperations (PairedCapturedListOperation pairs) leftOps rightOps)
    (left right : ExecutionState α) (initial : capturePairsRelated pairs left right) :
    (runExecution left leftOps).isSome = (runExecution right rightOps).isSome := by
  cases leftRun : runExecution left leftOps with
  | none =>
    cases rightRun : runExecution right rightOps with
    | none => rfl
    | some rightFinal =>
      obtain ⟨leftFinal, result, _⟩ := captured_pairs_sequence_simulates (pairs.map swapCapturePair)
        (swapped_capture_pairs_unique pairs unique) (swapped_capture_pairs_disjoint pairs disjoint)
        rightOps leftOps (swapped_capture_pair_operations pairs leftOps rightOps operations)
        right left rightFinal (swapped_capture_pairs_related pairs left right initial) rightRun
      rw [leftRun] at result
      cases result
  | some leftFinal =>
    obtain ⟨rightFinal, result, _⟩ := captured_pairs_sequence_simulates pairs unique disjoint leftOps rightOps
      operations left right leftFinal initial leftRun
    rw [result]
    rfl
