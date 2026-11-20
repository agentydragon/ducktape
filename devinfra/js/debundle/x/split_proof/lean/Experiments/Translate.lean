import Lean.Data.Json
import Std.Data.HashMap
import Std.Data.HashSet
import EsmSplit.Cli
import EsmSplit.Correct
import Experiments.Lower

/-!
# Is this output tree the bundle, renamed and reshuffled?

    translate <bundle.json> <output.json> <cert.json> [--table <file>]

`bundle.json` and `output.json` come from `experiments/translation/frontend.mjs`: the bundle's units (a unit is one
top-level statement or one declarator of a variable list) and the output tree's modules, with every identifier
annotated by what it means (`k`: local, module scope, import from outside the tree, global).
`cert.json` pairs every output unit with a bundle unit. It is untrusted: a wrong pairing is rejected here.

Checked:

1. The pairing is a bijection between bundle units and output units.
2. Paired units are equal up to renaming. Same shape, same literals and property names. Locals correspond by a
   bijection, globals are equal, module-scope names pair up. An import from outside the tree may stand for a
   member chain of another import (`ut(…)` against `Mobx.runInAction(…)`).
3. Links: the name a bundle declaration pairs with is the name the output module declares; every reference to a
   module-scope name resolves, through the output's imports and exports, to the declaration of the bundle name
   it pairs with.
4. The output entry exports every name the bundle exports, each meaning the same declaration. Extra exports are
   counted, not rejected.
5. The plan the output describes, written in the bundle's own names, passes `Sound`: unique definitions, linkable,
   hygienic, every unit once, conflicting units keep their order. A failure of the last is an *obligation* list.
6. ES imports are read-only: a unit assigns only names that its own module declares (`AssignsHome`, part of `Sound`).
7. Refused, because moving or renaming the unit changes what it does: `import.meta` (its value is the module's own;
   only `new URL("/absolute", import.meta.url)` is accepted, and listed as a premise), a direct `eval` (it reads
   module-scope names by their spelling) and a top-level `await` (module evaluation stops being a plain sequence).

Not checked here, and printed as premises: that an outside import and the member chain it was swapped for are the
same value, that a dynamic import's new path names the same chunk, and that `new Worker("/p")` and
`new Worker(new URL("/p", import.meta.url))` start the same worker (an absolute path on the same origin).
`new URL("/absolute", import.meta.url)` is accepted as it stands and listed as a premise: it resolves against the origin
only, and every module of the tree is assumed to be served from the bundle's origin.
These four are the only things the checker accepts besides renaming.
-/

open Lean EsmSplit

namespace Translate

def entries : Json → Array (String × Json)
  | .obj kvs => kvs.foldl (fun acc k v => acc.push (k, v)) #[]
  | _ => #[]

def fld (j : Json) (k : String) : Json := j.getObjValD k
def str (j : Json) : String := (j.getStr?).toOption.getD ""
def arr (j : Json) : Array Json := (j.getArr?).toOption.getD #[]
def nat (j : Json) : Nat := (j.getNat?).toOption.getD 0
def typ (j : Json) : String := str (fld j "type")

/-- What the lockstep comparison collects while it runs. -/
structure Ctx where
  /-- the output module being compared -/
  cur    : Nat := 0
  loc    : Std.HashMap Nat Nat := {}
  locRev : Std.HashMap Nat Nat := {}
  /-- (module, bundle name, output name, is a declaration) -/
  tops   : Array (Nat × String × String × Bool) := #[]
  /-- (module, bundle import, output root name, members) -/
  vends  : Array (Nat × String × String × Array String) := #[]
  /-- (module, path in the bundle, path in the output) of dynamic imports -/
  dyns   : Array (Nat × String × String) := #[]
  /-- paths of `new Worker("/p")` rewritten to `new Worker(new URL("/p", import.meta.url))` -/
  workers : Array String := #[]
  /-- paths of `new URL("/p", import.meta.url)` that stand unchanged -/
  metaUrls : Array String := #[]
  /-- how many functions enclose the node being compared -/
  depth : Nat := 0

abbrev M := ExceptT String (StateM Ctx)

/-- The outside import at the root of a chain of plain member accesses, and the members. -/
partial def chainRoot (j : Json) : Option (String × Array String) :=
  if typ j == "MemberExpression" && fld j "computed" == Json.bool false && fld j "optional" == Json.bool false then
    let p := fld j "property"
    let o := fld j "object"
    if typ p != "Name" then none else
    match typ o with
    | "Identifier" => if str (fld o "k") == "V" then some (str (fld o "name"), #[str (fld p "name")]) else none
    | "MemberExpression" => (chainRoot o).map fun (r, ms) => (r, ms.push (str (fld p "name")))
    | _ => none
  else none

def lastSegment (s : String) : String := (s.splitOn "/").getLast!

/-- The path `spec` names from a module at `path`, relative to the same root; `..` may leave the root. -/
def normalizeFrom (path spec : String) : String :=
  let segments := (path.splitOn "/").dropLast ++ spec.splitOn "/"
  let stack := segments.foldl (fun (acc : List String) seg =>
    if seg == "." || seg == "" then acc
    else if seg == ".." then (match acc with
      | [] => [".."]
      | ".." :: _ => ".." :: acc
      | _ :: rest => rest)
    else seg :: acc) []
  "/".intercalate stack.reverse

def isGlobal (j : Json) (name : String) : Bool :=
  typ j == "Identifier" && str (fld j "k") == "G" && str (fld j "name") == name

/-- `new URL(lit, import.meta.url)` -/
def isUrlOfMetaUrl (j : Json) (lit : String) : Bool :=
  let args := arr (fld j "arguments")
  typ j == "NewExpression" && isGlobal (fld j "callee") "URL" && args.size == 2
    && typ args[0]! == "Literal" && str (fld args[0]! "lit") == lit
    && typ args[1]! == "MemberExpression" && fld args[1]! "computed" == Json.bool false
    && typ (fld args[1]! "object") == "MetaProperty" && str (fld (fld args[1]! "property") "name") == "url"

mutual

/-- `a` (bundle) and `b` (output) are equal up to renaming; the renamings are recorded in the state. -/
partial def unify (a b : Json) : M Unit := do
  match a, b with
  | .arr xs, .arr ys =>
    unless xs.size == ys.size do throw s!"lists of {xs.size} and {ys.size} elements"
    for i in [0:xs.size] do unify xs[i]! ys[i]!
  | .obj _, .obj _ => unifyNode a b
  | _, _ => unless a == b do throw s!"{a.compress.take 60} against {b.compress.take 60}"

partial def unifyFields (a b : Json) (skip : String) : M Unit := do
  let ea := entries a
  let eb := entries b
  unless ea.size == eb.size do throw s!"{typ a}: different fields"
  for i in [0:ea.size] do
    let (ka, va) := ea[i]!
    let (kb, vb) := eb[i]!
    unless ka == kb do throw s!"{typ a}: field {ka} against {kb}"
    unless ka == skip do unify va vb

partial def unifyNode (a b : Json) : M Unit := do
  let ta := typ a
  let tb := typ b
  if ta == "Identifier" && tb == "MemberExpression" then
    match chainRoot b with
    | some (root, ms) =>
      unless str (fld a "k") == "V" do throw s!"`{str (fld a "name")}` is not an import, the output has a member chain"
      modify fun c => { c with vends := c.vends.push (c.cur, str (fld a "name"), root, ms) }
    | none => throw s!"identifier `{str (fld a "name")}` against a member expression"
  else if ta != tb then throw s!"{ta} against {tb}"
  else if ta == "Identifier" then unifyId a b
  else if ta == "MetaProperty" && str (fld (fld a "meta") "name") == "import" then
    throw "`import.meta` is module-relative: a unit that uses it changes meaning when it moves"
  else if ta == "CallExpression" && isGlobal (fld a "callee") "eval" then
    throw "a direct `eval` reads module-scope names by their spelling"
  else if (ta == "AwaitExpression" || (ta == "ForOfStatement" && fld a "await" == Json.bool true)) && (← get).depth == 0 then
    throw "top-level `await` is not modeled: module evaluation is no longer a plain sequence"
  else if ta == "FunctionDeclaration" || ta == "FunctionExpression" || ta == "ArrowFunctionExpression" then
    modify fun c => { c with depth := c.depth + 1 }
    unifyFields a b ""
    modify fun c => { c with depth := c.depth - 1 }
  else if ta == "NewExpression" && isGlobal (fld a "callee") "URL" && (arr (fld a "arguments")).size == 2
      && isUrlOfMetaUrl a (str (fld (arr (fld a "arguments"))[0]! "lit"))
      && (str (fld (arr (fld a "arguments"))[0]! "lit")).startsWith "s:/"
      && isUrlOfMetaUrl b (str (fld (arr (fld a "arguments"))[0]! "lit")) then
    modify fun c => { c with metaUrls := c.metaUrls.push (str (fld (arr (fld a "arguments"))[0]! "lit")) }
  else if ta == "NewExpression" && isGlobal (fld a "callee") "Worker" && isGlobal (fld b "callee") "Worker" then
    let aa := arr (fld a "arguments")
    let ba := arr (fld b "arguments")
    let lit := if aa.size > 0 then str (fld aa[0]! "lit") else ""
    if aa.size > 0 && aa.size == ba.size && typ aa[0]! == "Literal" && lit.startsWith "s:/" && isUrlOfMetaUrl ba[0]! lit then
      modify fun c => { c with workers := c.workers.push (lit.drop 2).toString }
      for i in [1:aa.size] do unify aa[i]! ba[i]!
    else unifyFields a b ""
  else if ta == "ImportExpression" then
    let sa := fld a "source"
    let sb := fld b "source"
    if typ sa == "Literal" && typ sb == "Literal" then
      let la := str (fld sa "lit")
      let lb := str (fld sb "lit")
      unless la.startsWith "s:" && lb.startsWith "s:" do throw "dynamic import of something that is not a string"
      unless lastSegment la == lastSegment lb do throw s!"dynamic imports of different files: {la} against {lb}"
      modify fun c => { c with dyns := c.dyns.push (c.cur, (la.drop 2).toString, (lb.drop 2).toString) }
      unifyFields a b "source"
    else unifyFields a b ""
  else unifyFields a b ""

partial def unifyId (a b : Json) : M Unit := do
  let ka := str (fld a "k")
  let kb := str (fld b "k")
  let na := str (fld a "name")
  let nb := str (fld b "name")
  unless ka == kb do throw s!"`{na}` is {ka} but `{nb}` is {kb}"
  unless fld a "d" == fld b "d" do throw s!"`{na}` and `{nb}` differ in being a declaration"
  match ka with
  | "G" => unless na == nb do throw s!"globals `{na}` and `{nb}`"
  | "L" =>
    let ia := nat (fld a "b")
    let ib := nat (fld b "b")
    let c ← get
    match c.loc[ia]?, c.locRev[ib]? with
    | some x, some y => unless x == ib && y == ia do throw s!"local `{na}` / `{nb}` bound differently"
    | none, none => set { c with loc := c.loc.insert ia ib, locRev := c.locRev.insert ib ia }
    | _, _ => throw s!"local `{na}` / `{nb}` bound differently"
  | "T" => modify fun c => { c with tops := c.tops.push (c.cur, na, nb, fld a "d" != Json.null) }
  | "V" => modify fun c => { c with vends := c.vends.push (c.cur, na, nb, #[]) }
  | _ => throw s!"unknown identifier class {ka}"

end

/-- What one output module says about its names. -/
structure ModInfo where
  path    : String
  decls   : Std.HashSet String := {}
  /-- local name -> (source, imported name) -/
  imports : Std.HashMap String (String × String) := {}
  /-- exported name -> local name -/
  exports : Std.HashMap String String := {}
  deriving Inhabited

/-- The declaration, as (module index, name in that module), that `y` means inside module `m`. -/
def resolveIn (mods : Array ModInfo) (index : Std.HashMap String Nat) (m : Nat) (y : String) : Except String (Nat × String) := do
  let mi := mods[m]!
  if mi.decls.contains y then return (m, y)
  match mi.imports[y]? with
  | none => throw s!"{mi.path}: `{y}` is neither declared nor imported"
  | some (src, imported) =>
    if src.startsWith "ext:" then throw s!"{mi.path}: `{y}` is imported from outside the tree ({src})"
    match index[src]? with
    | none => throw s!"{mi.path}: imports from {src}, which is not in the tree"
    | some s =>
      match mods[s]!.exports[imported]? with
      | none => throw s!"{src} does not export `{imported}`, imported by {mi.path}"
      | some z =>
        if mods[s]!.decls.contains z then return (s, z)
        else throw s!"{src} exports `{imported}` as `{z}`, which it does not declare"

structure Result where
  plan    : Plan
  names   : Nat
  /-- bundle import (from, name) -> output (module, imported name, members) -/
  vendor  : Array (String × String × String × String × Array String)
  dynamic : Array (String × String)
  workers : Array String
  metaUrls : Array String
  /-- exports of the output entry beyond the bundle's -/
  extraExports : Nat

def translate (bundleJ outJ certJ : Json) : Except String Result := do
  let bUnits := arr (fld bundleJ "units")
  let mods := arr (fld outJ "modules")
  -- 1. the pairing
  let mut triples : Array (Nat × Nat × Nat) := #[]
  let mut pairOf : Array (Option (Nat × Nat)) := Array.replicate bUnits.size none
  let mut usedOut : Std.HashSet (Nat × Nat) := {}
  for p in arr (fld certJ "pairs") do
    let pa := arr p
    let m := nat pa[0]!
    let u := nat pa[1]!
    let i := nat pa[2]!
    unless i < bUnits.size do throw s!"certificate names bundle unit {i}, the bundle has {bUnits.size}"
    unless m < mods.size && u < (arr (fld mods[m]! "units")).size do throw s!"certificate names output unit {m}/{u}, which does not exist"
    if pairOf[i]!.isSome then throw s!"bundle unit {i} is paired twice"
    if usedOut.contains (m, u) then throw s!"output unit {m}/{u} is paired twice"
    pairOf := pairOf.set! i (some (m, u))
    usedOut := usedOut.insert (m, u)
    triples := triples.push (m, u, i)
  for i in [0:bUnits.size] do
    if pairOf[i]!.isNone then throw s!"bundle unit {i} has no unit in the output"
  let outCount := mods.foldl (fun n m => n + (arr (fld m "units")).size) 0
  unless outCount == bUnits.size do throw s!"the output has {outCount} units, the bundle {bUnits.size}"
  -- 2. equal up to renaming
  let step : M Unit := do
    for (m, u, i) in triples do
      modify fun c => { c with cur := m, loc := {}, locRev := {} }
      let a := bUnits[i]!
      let b := (arr (fld mods[m]! "units"))[u]!
      try unify a b
      catch e => throw s!"bundle unit {i} against {str (fld mods[m]! "path")} unit {u}: {e}"
  let (r, ctx) := (step.run).run {}
  match r with
  | .error e => throw e
  | .ok _ => pure ()
  -- the output's symbol tables
  let mut infos : Array ModInfo := #[]
  let mut index : Std.HashMap String Nat := {}
  for mj in mods do
    let path := str (fld mj "path")
    let mut mi : ModInfo := { path }
    for d in arr (fld mj "imports") do
      for s in arr (fld d "specs") do
        mi := { mi with imports := mi.imports.insert (str (fld s "local")) (str (fld d "from"), str (fld s "imported")) }
    for e in arr (fld mj "exports") do
      mi := { mi with exports := mi.exports.insert (str (fld e "exported")) (str (fld e "local")) }
    index := index.insert path infos.size
    infos := infos.push mi
  -- 3. links. Declarations first: they say which output name each bundle name became.
  let mut entity : Std.HashMap String (Nat × String) := {}
  let mut nameOf : Std.HashMap (Nat × String) String := {}
  for (m, x, y, isDecl) in ctx.tops do
    if isDecl then
      match entity[x]? with
      | some e => unless e == (m, y) do throw s!"`{x}` is declared as {infos[e.1]!.path}:{e.2} and as {infos[m]!.path}:{y}"
      | none =>
        match nameOf[(m, y)]? with
        | some x' => unless x' == x do throw s!"{infos[m]!.path}:{y} stands for both `{x}` and `{x'}`"
        | none => pure ()
        entity := entity.insert x (m, y)
        nameOf := nameOf.insert (m, y) x
        infos := infos.set! m { infos[m]! with decls := infos[m]!.decls.insert y }
  for (m, x, y, isDecl) in ctx.tops do
    unless isDecl do
      let some want := entity[x]? | throw s!"`{x}` is used but no bundle unit declares it"
      let got ← resolveIn infos index m y
      unless got == want do
        throw s!"{infos[m]!.path}: `{y}` means {infos[got.1]!.path}:{got.2}, but it stands for `{x}`, declared as {infos[want.1]!.path}:{want.2}"
  -- the imports from outside the tree: a function from bundle imports to output expressions
  let bundleImports : Std.HashMap String (String × String) :=
    (arr (fld bundleJ "imports")).foldl (fun acc i => acc.insert (str (fld i "local")) (str (fld i "from"), str (fld i "imported"))) {}
  let mut vendor : Std.HashMap (String × String) (String × String × Array String) := {}
  for (m, alias, root, ms) in ctx.vends do
    let some bi := bundleImports[alias]? | throw s!"`{alias}` is not an import of the bundle"
    let some (src, imported) := infos[m]!.imports[root]? | throw s!"{infos[m]!.path}: `{root}` is not an import"
    unless src.startsWith "ext:" do throw s!"{infos[m]!.path}: `{root}` stands for the import `{alias}` but comes from the tree ({src})"
    let want := (src, imported, ms)
    match vendor[bi]? with
    | some w => unless w == want do throw s!"the import `{alias}` ({bi.1}#{bi.2}) stands for two different things in the output"
    | none => vendor := vendor.insert bi want
  -- the output's paths are relative to each module, so compare where they lead
  let mut dyn : Std.HashMap String String := {}
  for (m, x, y) in ctx.dyns do
    let y := normalizeFrom infos[m]!.path y
    match dyn[x]? with
    | some y' => unless y' == y do throw s!"dynamic import of {x} is rewritten to both {y'} and {y}"
    | none => dyn := dyn.insert x y
  -- the entry's export list is the chunk's public surface: every name the bundle exports must be exported under the
  -- same name and mean the same declaration. Extra exports (aliases under new names) only widen the surface.
  let some entryIdx := index[str (fld outJ "entry")]? | throw s!"the entry {str (fld outJ "entry")} is not in the output"
  let bundleExports := arr (fld bundleJ "exports")
  for e in bundleExports do
    let ex := str (fld e "exported")
    let x := str (fld e "local")
    let some want := entity[x]? | throw s!"the bundle exports `{ex}` = `{x}`, which no unit declares"
    let some z := infos[entryIdx]!.exports[ex]? | throw s!"the output entry does not export `{ex}`, which the bundle exports"
    let got ← resolveIn infos index entryIdx z
    unless got == want do throw s!"the output entry exports `{ex}` as {infos[got.1]!.path}:{got.2}, but the bundle exports `{x}`, declared as {infos[want.1]!.path}:{want.2}"
  for (_, z) in infos[entryIdx]!.exports.toList do
    let _ ← resolveIn infos index entryIdx z
  let extraExports := infos[entryIdx]!.exports.size - bundleExports.size
  -- 4. the plan, in the bundle's names
  let mut planMods : Array EsmSplit.Module := #[]
  for mi in [0:mods.size] do
    let mj := mods[mi]!
    let stmts := (triples.filter fun (m, _, _) => m == mi).map (fun (_, u, i) => (u, i)) |>.qsort (fun a b => a.1 < b.1) |>.map (·.2)
    let mut imps : List (ModName × List EsmSplit.Name) := []
    for d in arr (fld mj "imports") do
      let src := str (fld d "from")
      unless src.startsWith "ext:" do
        let mut names : List EsmSplit.Name := []
        for s in arr (fld d "specs") do
          let some si := index[src]? | throw s!"{infos[mi]!.path}: imports from {src}, which is not in the tree"
          let some z := infos[si]!.exports[str (fld s "imported")]? | throw s!"{src} does not export `{str (fld s "imported")}`"
          let some x := nameOf[(si, z)]? | throw s!"{src}:{z} stands for no bundle name"
          names := names ++ [x]
        imps := imps ++ [(src, names)]
    let mut exps : List EsmSplit.Name := []
    for e in arr (fld mj "exports") do
      let z := str (fld e "local")
      -- the entry may re-export what it imports; that is checked against the bundle's exports above
      unless mi == entryIdx && !(infos[mi]!.decls.contains z) do
        let some x := nameOf[(mi, z)]? | throw s!"{infos[mi]!.path} exports `{z}`, which stands for no bundle name"
        exps := exps ++ [x]
    planMods := planMods.push { name := infos[mi]!.path, stmts := stmts.toList, imports := imps, exports := exps }
  return { plan := { entry := str (fld outJ "entry"), mods := planMods.toList }
           names := entity.size
           vendor := vendor.fold (fun acc k v => acc.push (k.1, k.2, v.1, v.2.1, v.2.2)) #[]
           dynamic := dyn.fold (fun acc k v => acc.push (k, v)) #[]
           workers := ctx.workers
           metaUrls := ctx.metaUrls
           extraExports := extraExports }

/-- The bundle as the checker sees it: one statement per unit, footprints from `Lower`. -/
def bundleOfFootprints (fps : Array Lower.Footprint) : Bundle :=
  fps.toList.map fun f =>
    { src := "", defs := f.defs, writes := f.writes, assigns := f.assigns, eager := f.eager, lazy := f.lazy, hoisted := f.hoisted, effect := f.effect }

def bundleOf (units : Array Json) : Bundle :=
  bundleOfFootprints (Lower.finalize (units.map Lower.analyzeTop))

/-- Pairs of statements that conflict and that the plan runs in the opposite order from the bundle: what `OrderOk` rejects. -/
def conflictInversions (B : Bundle) (P : Plan) : Nat × Array (Nat × Nat) := Id.run do
  let arr := B.toArray
  let tab := B.readsTab
  let ord := P.execOrder.toArray
  let mut count := 0
  let mut sample : Array (Nat × Nat) := #[]
  for x in [0:ord.size] do
    for y in [x + 1:ord.size] do
      let a := ord[x]!
      let b := ord[y]!
      if b < a && Bundle.conflictT arr tab b a then
        count := count + 1
        if sample.size < 5 then sample := sample.push (a, b)
  return (count, sample)

def main (args : List String) : IO UInt32 := do
  let (table?, args) := match args with
    | [b, o, c, "--table", t] => (some t, [b, o, c])
    | l => (none, l)
  let [bundlePath, outPath, certPath] := args | IO.eprintln "usage: translate <bundle.json> <output.json> <cert.json> [--table <file>]"; return 2
  let t0 ← IO.monoMsNow
  let read (p : String) : IO Json := do IO.ofExcept (Json.parse (← IO.FS.readFile p))
  let bundleJ ← read bundlePath
  let outJ ← read outPath
  let certJ ← read certPath
  IO.println s!"  [time] read + parse JSON: {(← IO.monoMsNow) - t0} ms"
  let t1 ← IO.monoMsNow
  let r ← IO.lazyPure fun _ => translate bundleJ outJ certJ
  match r with
  | .error e => IO.println s!"TRANSLATION REJECT: {e}"; return 1
  | .ok res =>
    IO.println s!"  [time] pairing, equality, links: {(← IO.monoMsNow) - t1} ms"
    let units := arr (fld bundleJ "units")
    IO.println s!"TRANSLATION OK: {units.size} units, {res.plan.mods.length} modules, {res.names} module-scope names linked"
    IO.println s!"  premises: {res.vendor.size} outside imports stand for member chains or other imports; {res.dynamic.size} dynamic-import paths moved; {res.workers.size} worker paths made relative to import.meta.url; {res.metaUrls.size} absolute URLs against import.meta.url left as they are; {res.extraExports} extra exported aliases"
    if let some t := table? then
      let rows := res.vendor.map fun (bf, bi, os, oi, ms) => Json.mkObj [("bundle", s!"{bf}#{bi}"), ("output", s!"{os}#{oi}{ms.foldl (· ++ "." ++ ·) ""}")]
      IO.FS.writeFile t (Json.mkObj [("vendor", Json.arr rows), ("dynamic", Json.arr (res.dynamic.map fun (a, b) => Json.mkObj [("from", a), ("to", b)])), ("workers", Json.arr (res.workers.map Json.str)), ("urls", Json.arr (res.metaUrls.map Json.str)), ("extraExports", Json.num res.extraExports)]).pretty
      IO.println s!"  wrote the premise table to {t}"
    let t2 ← IO.monoMsNow
    let (fps, identityScanExhausted) ← IO.lazyPure fun _ => Lower.finalizeWithScanStatus (units.map Lower.analyzeTop)
    if identityScanExhausted then
      IO.println "  identity inference disabled: annotated JSON exceeds scan depth limit"
    let B := bundleOfFootprints fps
    for i in [:fps.size] do
      if let some f := fps[i]? then
        if let some name := f.inferredIdentity then
          IO.println s!"  inferred identity call: unit {i}, binding `{name}` (trusted source analysis)"
    IO.println s!"  [time] footprints: {(← IO.monoMsNow) - t2} ms; {(B.filter (·.effect)).length} of {B.length} units are effects"
    let P := res.plan
    let order ← IO.lazyPure fun _ => P.evalOrder
    IO.println s!"  modules evaluated from the entry: {order.length} of {P.mods.length}"
    let rs := [← timedCheck "UniqueDefs" fun _ => decide (UniqueDefs B),
               ← timedCheck "ReadsClosed" fun _ => decide (ReadsClosed B),
               ← timedCheck "Linkable" fun _ => decide (Linkable B P),
               ← timedCheck "Hygienic" fun _ => decide (Hygienic B P),
               ← timedCheck "AssignsHome" fun _ => decide (AssignsHome B P),
               ← timedCheck "Permutes" fun _ => decide (Permutes B P)]
    let ordered ← timedCheck "OrderOk" fun _ => decide (OrderOk B P)
    if rs.all id && ordered then IO.println "PLAN SOUND"; return 0
    if !rs.all id then IO.println "PLAN NOT SOUND: a structural check failed"; return 1
    let (n, sample) ← IO.lazyPure fun _ => conflictInversions B P
    IO.println s!"OBLIGATIONS: {n} pairs of conflicting units run in the opposite order from the bundle"
    for (a, b) in sample do IO.println s!"    unit {b} came first in the bundle; the output runs unit {a} before it"
    return 4

end Translate
