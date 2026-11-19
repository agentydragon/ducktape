import EsmSplit.Split
import EsmSplit.Correct
import Lean.Data.Json

/-!
# Input + output -> confirmation

No spec, no splitter. The only things read from the output files are, per module:
its `import` declarations, its `export` list, and the text of each remaining statement.
Lean matches those texts against the bundle's statements itself, so "the same code, moved"
is checked here and not trusted to the parser.
-/

open Lean

namespace EsmSplit

/-- Facts from before `writes` and `assigns` existed do not mention them: they default to none. -/
instance : FromJson Stmt where
  fromJson? j := do
    let names (k : String) : Except String (List Name) :=
      match j.getObjVal? k with
      | .ok v => fromJson? v
      | .error _ => pure []
    pure { src := ← j.getObjValAs? String "src", defs := ← j.getObjValAs? (List Name) "defs",
           eager := ← j.getObjValAs? (List Name) "eager", lazy := ← j.getObjValAs? (List Name) "lazy",
           writes := ← names "writes", assigns := ← names "assigns",
           hoisted := ← j.getObjValAs? Bool "hoisted", effect := ← j.getObjValAs? Bool "effect" }

/-- One output file, as read from disk. -/
structure OutModule where
  name    : ModName
  imports : List (ModName × List Name)
  exports : List Name
  /-- statements in file order, as source text -/
  body    : List String
  deriving Repr, FromJson

/-- Take each text from the pool of bundle statements not yet used. -/
def takeStmts (B : Bundle) : List String → List Nat → Option (List Nat × List Nat)
  | [], pool => some ([], pool)
  | t :: ts, pool =>
    match pool.find? fun i => (B.stmt i).src == t with
    | none => none
    | some i => (takeStmts B ts (pool.erase i)).map fun (ids, rest) => (i :: ids, rest)

/-- Recover the plan from the output files: every statement text must be a distinct bundle
statement, and every bundle statement must be used. -/
def matchOutput (B : Bundle) (entry : ModName) (outs : List OutModule) : Option Plan :=
  go outs (List.range B.length) []
where
  go : List OutModule → List Nat → List Module → Option Plan
    | [], pool, acc => if pool.isEmpty then some { entry, mods := acc.reverse } else none
    | o :: os, pool, acc => do
      let (ids, pool') ← takeStmts B o.body pool
      go os pool' ({ name := o.name, stmts := ids, imports := o.imports, exports := o.exports } :: acc)

/-- The recovered plan says what the files say. -/
def Matches (B : Bundle) (entry : ModName) (outs : List OutModule) (P : Plan) : Prop :=
  P.entry = entry
  ∧ P.mods.map (·.name) = outs.map (·.name)
  ∧ P.mods.map (·.imports) = outs.map (·.imports)
  ∧ P.mods.map (·.exports) = outs.map (·.exports)
  ∧ P.mods.map (fun m => m.stmts.map fun i => (B.stmt i).src) = outs.map (·.body)
  ∧ (P.mods.flatMap (·.stmts)).Nodup
  ∧ ∀ i ∈ List.range B.length, i ∈ P.mods.flatMap (·.stmts)

/-- `Valid` without the spec: everything the semantic theorem and the linking need. -/
def Sound (B : Bundle) (P : Plan) : Prop :=
  UniqueDefs B ∧ ReadsClosed B ∧ Linkable B P ∧ Hygienic B P ∧ AssignsHome B P ∧ Permutes B P ∧ OrderOk B P

instance (B : Bundle) (entry : ModName) (outs : List OutModule) (P : Plan) :
    Decidable (Matches B entry outs P) := by unfold Matches; infer_instance
instance (B : Bundle) (P : Plan) : Decidable (Sound B P) := by unfold Sound; infer_instance

theorem sound_correct {B : Bundle} {P : Plan} (h : Sound B P) : Correct B P := by
  obtain ⟨_, _, _, hyg, _, ⟨hnd, hlt, hall⟩, hord⟩ := h
  have hmem : ∀ x, x ∈ P.execOrder ↔ x < B.length :=
    fun x => ⟨hlt x, fun hx => hall x (List.mem_range.2 hx)⟩
  exact ⟨⟨hnd, hmem⟩, hyg, fun _ _ M s0 => B.exec_reorder M s0 B.length P.execOrder hnd hmem hord⟩

def checkOutput (B : Bundle) (entry : ModName) (outs : List OutModule) : Bool :=
  match matchOutput B entry outs with
  | some P => decide (Matches B entry outs P ∧ Sound B P)
  | none => false

/-- If the check passes, the files are a reshuffle of the bundle: the plan they describe
holds exactly the bundle's statements (verbatim), and it is `Correct`. -/
theorem checkOutput_correct {B : Bundle} {entry : ModName} {outs : List OutModule}
    (h : checkOutput B entry outs = true) : ∃ P, Matches B entry outs P ∧ Correct B P := by
  unfold checkOutput at h
  split at h
  · rename_i P _
    exact ⟨P, (of_decide_eq_true h).1, sound_correct (of_decide_eq_true h).2⟩
  · exact absurd h (by simp)

end EsmSplit
