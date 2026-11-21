# Lessons from the split-proof experiment

Recorded on 2026-11-21, after taking the experiment from a 12-statement example to a 9,707-unit production bundle. The contracts are in [`../README.md`](../README.md); the work that follows from these lessons is in [`../PLAN.md`](../PLAN.md). This file records what broke, what misled, and what the measurements said.

## The proved part held, and everything around it leaked

- **No wrong accept came from the proved step.** `Valid B S P` implies `Correct B P` for every bundle, and nothing we found contradicts it. Every wrong accept came from an input the checker trusts.
- **The model lacked rules that only real code showed.** Each was found by running accepted output in Node, never by reading the model:
  - an `export` statement outside the entry module, found by the split experiment ([`../experiments/README.md`](../experiments/README.md) § 7);
  - a glue export that repeats a name the entry already publishes, found by debundle's corpus (§ 11), whose first run accepted two splits that fail to load;
  - code that assigns a name in a module that only imports it, found by a probe where Node threw on an accepted split (now `AssignsHome`);
  - a pure read moving across a write, and `import.meta`, both found by the translation fuzzer.
- **So an accepted plan needs Node, not only Lean.** A test that ends with a verdict misses this class. Every fixture should end with Node running the output and comparing it with the bundle.

## The footprint analysis is the weak input

The checker reads one footprint per unit: what it declares, reads, may write, and whether it touches the world. [`Effects.lean`](../lean/Experiments/Effects.lean) computes them by abstract interpretation. It is about 2,150 lines, tested and not proved.

- **Every independent attack found dozens of wrong rules.** Three attempts of about half an hour each found 24 wrong accepts (patches of standard objects), then 63 (the table of standard functions), then 29 (the core language). Each looked at a different area and each batch was fixed, so the counts do not show a trend; they show that no area we looked at was clean.
- **Most units only allocate.** 7,123 of the large bundle's 9,707 top-level units are built from literals, object and array literals, function expressions and plain classes, with no call and no property read. The analysis calls none of them an effect. A rule for this one shape, proved against a model, would cover 73% of the units.
- **A footprint oracle finds what reorder tests miss.** [`footprint_oracle.mjs`](../experiments/translation/footprint_oracle.mjs) runs a program unit by unit in Node and compares what each unit changed with its footprint. It found F1 to F3 in about 10,000 units ([`generator_findings`](../experiments/translation/debug/generator_findings/README.md)). A reorder test sees a wrong footprint only if a random reorder moves the one reader that matters; for F4 the end-to-end fuzzer found nothing in the same programs (reported in ducktape#18429).
- **Every new part of the language the generator reached exposed a wrong rule.** Iterator helpers gave F1, long prototype chains F2, `Array(n)` F3. Classes and optional chaining gave F4 (`delete n?.b` writes nothing) and F5 (a stack overflow on a two-unit program) in ducktape#18429.
- **Two kinds of error, with different cures.** F2 and F5 are bugs in the engine (a depth cap, the recursion). F1 and F3 are wrong rows in the table of standard functions. A proof against a semantics removes the first kind. It removes the second only if the standard functions are defined in the semantics and not asserted in a table.
- **Fail-safe needs care at every cap.** F2 and the audit of the give-up paths found give-ups that forgot what the skipped code could reach: a depth limit that returned "unknown value" without running or flagging the accessors behind it. A cap must degrade to "runs unknown code", never to "nothing happened".

## The first slice of a model

[`CoreJS`](../lean/Experiments/CoreJS/) is an executable model of a core of JavaScript, with one footprint rule proved in it ([`../experiments/README.md`](../experiments/README.md) § 14).

- **An interpreter can be tested, and the test found the one error.** Model and Node agree on every program both run. The one disagreement on the way was the last bit of `**`, which the specification leaves to the implementation; the operator is refused now.
- **The program generator barely reaches the model.** No whole generated program is in the fragment, because nearly every one calls the standard library. Cut down to the units the model accepts, about 9% of the units are left. Hand-written cases carry the object model. A few methods of strings and arrays would let whole programs through.
- **Where an object is allocated decides how hard commutation is.** With one counter, two units in the other order make the same objects at other addresses, and "the same state" needs a renaming. The model gives each unit its own region to allocate in, which JavaScript cannot observe, and the two orders end in equal states.
- **Unknown code has to be an outcome of the model.** A run ends with a value, a thrown value, or "reached code the model does not define". That keeps the fragment honest: nothing outside it gets a guessed result.

## What the large bundle says

Numbers are for one production bundle of 9,707 units and its reviewed tree of 2,793 modules, under premise P3. The evidence is in the private repository.

- **Unknown code dominates.** 1,584 units touch the world. 1,558 of them run unknown code, and each is taken to write about 7,800 names: every import and every name that escapes anywhere in the bundle. 98.5% of the 670,997 order obligations involve one such unit.
- **A few imports cause most of it.** 35 vendor imports account for 978 of the 1,584 units. About 450 units are decorator applications.
- **Counts from different analyses do not compare.** An earlier analysis reported 337,167 obligations for the same tree. It scanned with much smaller write sets, so the lower number did not mean more was proved.
- **The tool is useful today for simple modules.** 873 of the 2,793 modules (31%) can be peeled alone with no order obligation. 812 of them hold only function declarations, which are hoisted and conflict with nothing. A script that reproduces the checker's conflict rule gave these counts; `lowersplit` agreed with it on the 13 single peels we ran.
- **Most of the tree is valid today if the modules run in bundle order.** 2,367 of the 2,793 modules (64% of the units, 58% of the source) can be kept whole with no order obligation, and `lowersplit` accepts that split. 2,131 of them are one contiguous run of the bundle: cutting a bundle into consecutive pieces and running them in the same order is valid whatever the pieces do, so these need no footprint at all. The other 236 are scattered modules that fit one slot between the units they conflict with. Five more would fit, but each separates an assignment from the declaration it assigns, which has to stay in one module.
- **The planner, not the analysis, loses that order.** `mkPlan` derives imports from the names a module mentions, and ES evaluation order follows the imports. For the partition above that inverts 311,493 conflicting pairs, or 1,130,007 with `order` given. Without an order, 886 modules peel together (18% of the units). A spec with `sequential` now keeps the order: a module that an earlier one mentions imports that earlier module first, and every module between. That nests 466 deep on this bundle and adds ordering imports to 629 of 3,049 modules. The simpler chain, where every module imports its predecessor, is as deep as there are modules, and Node fails to load a chain somewhere between 3,000 and 10,000 deep.
- **The accepted split behaves like the bundle in a browser.** The 3,049 modules were put in place of the main chunk and both versions loaded in headless Chromium with the network cut off: the same page, the same requests and the same console lines, up to stack traces, which name the new files. That checks start-up and the first render, not use of the application. The split loads 3,054 files where the original loads 6.
- **So footprints are needed to remove order, not to keep it.** With the order kept explicitly, a split is valid first, and each ordering edge can then be dropped where a footprint shows it is not needed. An obligation is a conflicting pair that the split runs in the other order, so the counts above measure how far a split's order is from the bundle's as much as how much is unknown.
- **The biggest blocker was not the analysis.** 1,318 modules (47%) failed `ExportNames`: the glue export collided with an alias in the entry's own export list. Real chunks also needed a workaround, because `lowersplit` refused import statements and could not split `const a = …, b = …` between modules (675 such statements held 5,068 declarators). Both are fixed: export names are in the model (`ExportsUnique`), and the dump splits declarator lists and keeps imports.
- **A vendor table alone changes nothing at module level.** With 40 imports assumed harmless, the obligations halve, and the count of modules that peel alone stays at 873. A unit unlocks only when every call in it is known, and the unknown-code units that remain still write every escaped name.
- **The largest lever is unproved.** Not counting names declared later as written by earlier unknown code would lift 873 to about 1,624. This is a candidate rule, and the count is optimistic: a name bound later to an object that escaped earlier must stay in, and the estimate drops it. A prototype of the sound version did not finish on the bundle in 15 minutes.
- **Library code is out of reach.** On the 20,032-unit vendor chunk `lower` aborts with a stack overflow. In the first 3,000 units, one store to `window.Promise` makes every unit that is not a function declaration touch the world.

## What the inputs require

- **Nearly the whole language.** The inputs use classes with private members and static blocks, generators, async generators, `for await`, `using` and tagged templates ([`../SUPPORT.md`](../SUPPORT.md)).
- **Nearly the whole standard library.** The names of all but 37 of the 378 members of Node 22's standard objects occur in the inputs, with 226 web platform globals and 42 host modules ([`../HOST_FEATURES.md`](../HOST_FEATURES.md)).
- **Patches of standard objects are real.** The inputs assign `Object.prototype.toString`, `Function.prototype.toString`, `Array.prototype.reduce` and four more by name. Premise P1 cannot be "nobody patches"; it has to be "the analysis sees the patches".
- **So no small fragment covers the inputs.** A model of JavaScript can be precise only on a core. Everything else has to count as unknown code under one stated assumption.

## How we worked

- **Measure before building.** The probe of what peels today cost one agent run and reordered the work: two plain engineering fixes came out ahead of every precision rule.
- **Re-run the evidence at the merged commit.** Numbers moved between a pilot binary and the merged one (1,557 against 1,558 units), and a count quoted from a pilot went stale in a merged description.
- **Label an estimate as an estimate, and keep it out of the docs until it is checked.** Twice a sentence claimed more than was shown ("cannot have escaped") and had to be corrected.
- **An independent attempt is cheap and worth repeating** after every change to the interpreter: about half an hour of an agent's time found dozens of wrong accepts each round.
- **Private inputs, public results.** Reports that reach this repository print only names that are public already and count the rest; fixtures use neutral names.
