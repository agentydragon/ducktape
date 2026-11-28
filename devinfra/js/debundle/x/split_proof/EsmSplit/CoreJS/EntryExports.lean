import EsmSplit.CoreJS.Value
import Std.Data.HashMap

/-! Entry namespace diagnostics use the public name and the resolved live cell.
Only a failed permutation check builds the comparison indexes and examples. -/

namespace CoreJS.EntryExports

abbrev Bindings :=
  List (Name × Addr)

def resolve (entry : String) (linked : List (Module × Env)) : Except String Bindings := do
  let some (m, env) := linked.find? (fun (m, _) => m.name == entry) | throw "missing linked entry"
  m.exports.mapM fun (publicName, localName) => do
      let some cell := env.vars.lookup localName | throw s!"unbound entry export {publicName}"
      return (publicName, cell)

private def label (name : Name) : String :=
  if name.length ≤ 80 then name.quote else (name.take 80).toString.quote ++ "…"

private def cell (a : Addr) : String :=
  s!"@{a.region}.{a.index}"

private structure Examples where
  count : Nat := 0
  shown : Array String := #[]

private def Examples.add (xs : Examples) (describe : Unit → String) : Examples :=
  ⟨xs.count + 1, if xs.shown.size < 3 then xs.shown.push (describe ()) else xs.shown⟩

private def Examples.render (xs : Examples) (kind : String) : String :=
  let suffix := if xs.count > xs.shown.size then ", …" else ""
  if xs.count == 0 then s!"{kind} 0"
  else s!"{kind} {xs.count}: {String.intercalate ", " xs.shown.toList}{suffix}"

/-- Explain a rejection in declaration order. Whichever cells currently hold
the same value still differ when their addresses differ. -/
def mismatch (original candidate : Bindings) : String :=
  Id.run do
    let before := Std.HashMap.ofList original
    let after := Std.HashMap.ofList candidate
    let header := "entry exports resolve to different linked cells"
    -- The input linker checks uniqueness. Do not collapse duplicate keys into
    -- spurious name-level evidence for a malformed standalone program.
    if before.size != original.length || after.size != candidate.length then
      return header ++ ": repeated public export name"
    let mut extra : Examples := {}
    let mut missing : Examples := {}
    let mut changed : Examples := {}
    for (name, a) in original do
      match after[name]? with
      | none =>
        missing := missing.add fun _ => s!"{label name} (original cell {cell a})"
      | some b =>
        if a != b then
          changed :=
            changed.add fun _ =>
              s!"{label name} (original cell {cell a} -> candidate cell {cell b})"
    for (name, b) in candidate do
      unless before.contains name do
        extra := extra.add fun _ => s!"{label name} (candidate cell {cell b})"
    return String.intercalate "; "
        [header, extra.render "extra", missing.render "missing", changed.render "retargeted"]

end CoreJS.EntryExports
