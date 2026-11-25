# Plan

The goal is to turn the production bundle in `gaffer-private` into runnable, readable modules following its authored spec, with a machine-checked guarantee that the split behaves like the original.

## Acceptance contract

Given the original module graph and a proposed placement, Lean certifies the output module graph or refuses. A certification states the [target theorem](#target): each source unit sits where the placement puts it, the entry exports the same public names bound to the same live cells (including the effects of generated imports and exports), and in every world that satisfies the listed host premises, each successful final state of either program is a successful final state of the other. When the split keeps the original order, the verdict also reports that every modeled ending matches, including throws. Applying the result to JavaScript relies on the [trusted parts](#trusted-and-tested).

The production acceptance run uses the private bundle, its dependencies and the reviewed placement. Record the certified guarantee for the complete split, which authored boundaries it realizes, and the result of running the emitted application through its browser smoke test. Until it certifies, record the first reproducible blocker on that input. Keep the bundle, selectors and run artifacts in the private repository.

### Build the Lean placement spec from the existing spec

Keep the pinned original spec as the baseline. Derive the private Lean spec from its selectors, owner assignments and module order. Start with certified dependency-closed regions, then certify their unions through the ordinary splitter. A union can introduce initializer reversals that neither region exercised on its own.

When an authored placement changes observable behavior, save the smallest input and unchanged placement that exhibit the difference, and skip that extraction. An unsupported construct or a resource limit leaves the placement undecided. Track those cases for language modeling, proofs and optimization. Record any proposed change to ownership or order as an explicit diff from the original spec for review, and replay the resulting placement at emission and from disk.

## Production replay status

The 2026-11-22 survey of the private production bundle found that 2,766 of 9,707 body units lower individually. Of 2,793 authored modules, 161 lower every unit they own. For 141, lowering also succeeds across the dependencies found by the syntactic scan. Ten of those depend on another authored module. All ten now have linked CoreJS `normal-completion` certificates for dependency-closed **excerpts**. Eight exercise records and two call an imported formatter during initialization. The complete archived split remains uncertified.

**First certified authored reorder (2026-11-22).** One complete three-module extraction has two independent string providers and an entry that exports a record built from both bindings. The archived imports evaluate the two providers in reverse source order. The ordinary splitter preserves that crossing and certifies the linked extraction at `normal-completion`. Its `--check-output` run also passes. In Node, the original excerpt, emitted entry and archived entry agree on the sole public name and the record’s prototype, own key order, values and property descriptors. The original excerpt exposes the corresponding bundle binding under the archived entry’s public name.

Changing the second provider’s initializer to read the first binding keeps the same spec and makes the crossing observable. The source initializes normally. The ordinary checker refuses because the split throws during linked initialization and publishes no modules. Node raises a temporal-dead-zone `ReferenceError` in the diagnostic preview with the same module order. The existing checker covers this boundary without a host contract or proof extension.

**Two calls to a shared formatter (2026-11-22).** The archived schedule reverses two initializers that call the same function. A five-declaration excerpt preserves the authored owners of the formatter, its captured constants and the two calls. An observation entry exposes both caller bindings under their archived names. The ordinary splitter certifies linked `normal-completion` and passes `--check-output`. Node observes the same two strings in the source excerpt, emitted entry and actual archived caller modules. The certificate covers the selected provider declarations; the runtime comparison loads the full archived provider.

A control changes only the formatter body. It tries to read the first caller’s binding and falls back to the original computation if the binding is uninitialized. Both orders complete normally. In source order the second call sees the first result. With the calls reversed it computes a different value. The checker certifies the control in source order and refuses the authored reversal; Node confirms the difference on the emitted diagnostic preview.

**Complete authored closures (2026-11-22).** A second survey starts at each of the 2,804 archived main-chunk paths, follows static imports in evaluation order, and maps every declaration in those modules back to its original bundle position. It found 233 reachable multi-module closures without an external import, cycle, top-level await or lexically outer dynamic import. The largest has 52 body units. In 27 closures a cross-module initializer pair reverses in both the standalone closure and the main entry’s projected module order. This projection omits 26 generated vendor facade paths missing from the archive. The individual closure counts measure candidates for verification.

Two complete two-module closures now certify at linked `normal-completion` and pass `--check-output`. The first contains 11 declaration units. Allocation of the entry's exported predicate crosses four provider string initializers. All ten provider declarations retain their authored placement, and the entry exposes exactly the original binding under its archived public name. In Node, the original source extraction, archived entry and emitted entry agree on direct calls and on eight eligibility decisions made by the archived consumer callback. The second closure has seven declaration units and keeps initializer order. A hoisted function captures a dense array from its provider. Nine direct input cases and eleven calls through the archived URL consumer agree across all three versions. The certificates cover module initialization and the exported closure state. The calls made after initialization are runtime observations.

A control changes only the predicate initializer. It reads the first crossed provider binding, catches a temporal-dead-zone error in source order, and chooses a different predicate if the binding has already initialized. Both orders finish and expose the same single public name. The same-order control certifies. The checker refuses the authored reversal and publishes no files. With the same input, Node returns `true` from the original control and `false` from the diagnostic preview.

**Two allocating providers.** The ordinary checker certifies generated splits that evaluate two array allocations in reverse source order. It preserves which array an exported alias denotes. Changing an initializer to catch a temporal-dead-zone read lets both orders complete with different aliases. The checker refuses that crossing, and Node observes the difference. Each original statement keeps its allocation region when it moves between modules. The generated five-module test also certifies two providers that read a shared record and a hoisted builder that spreads their arrays. [Generated alias controls](translation/linked/arrays_test.mjs) and [five-module cases](translation/linked/composition_test.mjs).

The complete five-module production extraction now certifies at linked `normal-completion` with both allocating providers in their authored modules. Their array initializers exchange positions relative to the original bundle. The shared dictionary keeps all 705 fields. The ordinary splitter certifies its output ASTs before printing. A separate `--check-output` run repeats the proof and checks that the files on disk parse to those trees. The linked programs finish with the same complete state and expose the same public builder binding in every modeled world. Node calls the source, archived, and emitted builders and compares the complete returned object. Each call creates a fresh outer array while preserving the identity of the records it contains. The certificate covers initialization and the exported closure. The subsequent calls are runtime observations.

The control changes one array initializer to read the other provider. A temporal-dead-zone read falls back to a fresh, structurally equal record in source order. Under the archived placement it reaches the already initialized provider record instead. Both executions finish normally and return equal-looking data, but two positions alias only in the reversed version. The source-order control certifies. The ordinary checker refuses the same changed source under the original five-owner spec and publishes no files. Node confirms the identity difference in the forced diagnostic output.

The proof scales by giving the ordered string fields a private Lean definition whose equality to the complete literal is proved once. Kernel-checked allocation and lookup equations reuse that definition. The certificate still decodes the complete source and emitted ASTs and checks the exact linked programs. [Record evaluation theorem](https://github.com/agentydragon/ducktape/pull/19248) · [Certificate generator and 705-field regression](https://github.com/agentydragon/ducktape/pull/19251).

### Passing ownership subset

The manifest named `passing` selects proposed owners and leaves the residual code in the entry. Current Rust refuses placements in this set. Treat each requested move as a candidate for review. This is a different artifact from the archived review tree. On the pinned input the manifest selects 1,686 owners, with 8,069 named and 13 anonymous claims. The private target's pinned Rust engine resolved all 9,701 canonical selectors. A source-coordinate replay joined every claim to the original source without a conflicting retained owner. An attempt to build the exact target in the agent environment stopped during Bazel host libc detection, before the Rust gate ran. For any successful emission, check the entry namespace, dependency schedule, browser startup and linked certificate on those exact files. Historical measurements on the archived review entry cannot answer these questions.

**Passing-spec checkpoint.** At `1525fedd`, 4,594 of 8,082 selected units and 5,749 of 9,707 body units lower. Of 1,686 owners, 326 have every selected declaration lower, and 167 also have a lowerable transitive named-dependency closure. Of those 167, 131 retain all canonical ownership claims, including 130 whose dependencies are all selected. The previous checkpoint at `73451391` had 4,459 selected units and 158 closed owners. These counts measure translation. Certification requires the assembled module graph and its authored placement.

The exact two-owner mobile/file excerpt certifies on emission and from disk. Its two original functions are hoisted. A synthetic eager call in the consumer exercises their binding through the original owner-order reversal and the generated import cycle.

**Full 705-field, 44-row extraction.** The ordinary splitter certifies the four original declarations in two authored owners at linked `normal-completion`. A separate disk check reparses the emitted JavaScript and repeats the certificate. One scalar initializer crosses construction of the command table. Node agrees on all four entry bindings, all 705 own property descriptors, and all 44 command tuples. The certificate uses the default proof budget. Kernel-checked per-key lookup and dense-array reductions avoid expanding the same record scan and heap operations across the whole proof. [Public full-size fixture](https://app.buildbuddy.io/invocation/30744608-9fc0-49b3-abcb-ab6c74b60026) · [Generated cases](https://github.com/agentydragon/ducktape/pull/19344).

A control makes the crossed initializer write the ID captured by the first command. Both orders finish normally and expose the same public names. Node observes different captured IDs, and both strict checks reject the reversal for different final states.

The union with three additional authored consumers also certifies on emission and disk. It preserves all seven extraction bindings and two reversals between eager initializers. The existing browser-page profile admits a deferred Set constructor, and both receipts report an empty assumptions list. Node checks fresh Set and command-builder results and the identity of a record returned by the template filter. These calls exercise the resulting closures after the certified initialization. [Checked lookup](https://github.com/agentydragon/ducktape/pull/19347) · [Dense-array reduction](https://github.com/agentydragon/ducktape/pull/19349).

A seven-owner union adds two consumers that use the shared dictionary for UI mutations and search classification. It keeps eleven entry bindings and four reversals between eager initializers. The ordinary splitter certifies emission and reparsed output under the same profile. In Node, the source and split agree on receiver identity, a field update and the subsequent call to delete its tuple, attribute-scope decisions and logical operator names.

The six-owner parameter-options union retains all 22 additional declarations, 29 entry bindings and 68 reversals between eager initializers. The checker uses the bulk allocation equation for small string-valued records. Both strict checks certify linked normal completion at the default proof budget. Node matches all 29 exports, and both checks reject the behavior-changing control. The eight-owner union combines the options, search and appearance consumers. It retains 33 bindings and 70 eager reversals. Node matches the diagnostic preview. Strict emission reached the 1,830 MiB private replay guard before returning a verdict. [Small-record proof generation](https://github.com/agentydragon/ducktape/pull/19352) · [Public 68-reversal regression](https://app.buildbuddy.io/invocation/e99ca711-af69-4359-b3ec-e23a34629936).

**Full-source Lean replay.** A candidate assigned every one of the 9,709 source ordinals exactly once, across the 1,686 selected owners and the residual entry, and used the authored import priorities. The ordinary splitter parsed the complete source and requested the captured vendor module. After 79 seconds the replay's 1,400 MiB process-group memory guard stopped it before the vendor response. It emitted no files and returned no certification verdict. Repeat the private replay with more memory to measure whole-input progress. Checking the exact files emitted by the pinned Rust target remains a separate milestone.

**Lowering baseline.** Among the 8,082 explicitly selected units, the linked decoder accepted 3,210 in isolation at Ducktape revision `ccd89a2d`. It accepted another 715 of the 1,625 units without a passing claim. This census uses the main chunk's actual imported binding names and records the first refusal for each unit. It does not link the vendor graph or execute the initializers.

| First refusal among selected units | Units | Next question                                                                                                                                                                                                                                                                |
| ---------------------------------- | ----: | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Expected an identifier             | 1,294 | 759 object-pattern parameters, 337 object-pattern declarations, 134 default parameters, 58 array-pattern declarations, four rest parameters and two array-pattern parameters. Flat patterns in parameters and local declarations now use ordered property reads (see below). |
| Async or generator function        |   589 | Determine which bodies run during initialization before adding asynchronous execution semantics.                                                                                                                                                                             |
| `if` statement                     |   574 | Use the existing CoreJS conditional statement for supported function bodies and check the decoded program against the generated proof.                                                                                                                                       |
| Class declaration                  |   383 | Separate plain classes from inheritance, computed members and static initialization.                                                                                                                                                                                         |
| Logical expression                 |   380 | Use CoreJS short-circuit evaluation and test a reversal where evaluating the right operand changes the outcome.                                                                                                                                                              |

The function-body `if` and short-circuit changes together raised the isolated count to 3,360 selected units (4,087 overall) in a source replay. The individual gains were 21 and 108 selected units respectively; some units require both. [Function-body branches](https://github.com/agentydragon/ducktape/pull/19258) · [Short-circuit expressions](https://github.com/agentydragon/ducktape/pull/19259).

Flat object destructuring in parameters and local declarations raised isolated lowering from 3,360 to 3,421 of 8,082 selected units (4,151 of 9,707 overall). All previously lowered units still lowered. Among selected units whose previous first refusal was “expected an identifier,” the replay now reaches sequence expressions in 130, call expressions as object property values in 127, and optional chains in 123. These counts record the first diagnostic in each unit. An excerpt with two unchanged selected functions in their authored modules also passed the ordinary linked normal-completion and disk checks. The synthetic entry called both functions during initialization. A separate Node observation confirmed all six getter reads, in order and with their original receivers, on both versions.

Two selected functions from different passing owners now have an excerpt certificate. The source keeps both original function bodies and exposes the consumer under its reviewed name. Both the ordinary split and its disk check certify linked normal completion. Node agrees on six direct calls. A second certificate adds those calls as module initializers so the proof exercises the logical expressions. The owner modules contain other declarations outside this excerpt.

Sequence expressions and calls in static data properties raised isolated lowering from 3,421 to 3,628 of 8,082 selected units. On the same baseline, sequence expressions alone contributed 58 units, property-value calls alone contributed 139, and ten units required both. Supporting logical expressions in record fields added another 12, bringing the total to 3,640 selected and 4,591 of 9,707 overall, with no regressions. Among the 374 selected units previously blocked first by a sequence expression, 125 next reached an unsupported assignment after the first two changes. [Sequences](https://github.com/agentydragon/ducktape/pull/19267) · [Property-value calls](https://github.com/agentydragon/ducktape/pull/19266) · [Logical property values](https://github.com/agentydragon/ducktape/pull/19268).

The configuration excerpt preserves four original declarators, their complete dependency chain, and placement in two authored owner modules. Its initializer makes eight calls to its original lookup helper. The entry exposes the reviewed binding through a synthetic export list. Both the ordinary splitter and the emitted-file check certified linked normal completion. Node matched the eight field values, descriptors and key order. Reversing the owner modules failed certification and threw a `ReferenceError` in the forced preview. In a separate excerpt, a synthetic module initializer executes an unchanged four-operand sequence from a selected function. The ordinary and emitted-file checks certified it. Swapping two modules made both versions finish normally with different captured values. The checker rejected the swap, and Node reproduced the difference on the forced preview.

**Numeric enum acceptance case.** Simple member assignment raises isolated lowering to 3,675 of 8,082 selected units (+35) and 4,667 of 9,707 overall (+76), with zero regressions. The dependency-closed authored enum has 89 entries and 178 writes. Balanced comma expressions let both linked probe runs finish at fuel 32. Node agrees on the public binding and all 178 own properties, including order and descriptors, in the diagnostic split. [Member assignment](https://github.com/agentydragon/ducktape/pull/19274) · [Balanced sequences](https://github.com/agentydragon/ducktape/pull/19273).

Before batch evaluation, at the default reduction budget a public synthetic enum certified at 32 entries and exhausted the budget at 64. Raising the budget to two million steps certified the public 89-entry case. Checking the exact private proof at that setting exceeded the replay sandbox's 2 GiB limit.

The unmodified checker at `63ba30d5` ran three matched public splits with `--require normal-completion`. Each relocates an immediately invoked arrow function that returns its object argument. Each comma operand evaluates a string literal, performs two nested writes to the same two keys, or performs two nested writes that add new keys. Both write controls use string keys. The table reports peak sampled RSS for one process. [Public width and RSS run](https://app.buildbuddy.io/invocation/0e8dbc25-522a-42ae-b18c-6e2d2ab6c5d9).

| Comma operands | String literals | Two fixed keys |    Growing key set |
| -------------: | --------------: | -------------: | -----------------: |
|             16 |        1.39 GiB |       1.51 GiB |           1.64 GiB |
|             32 |        1.40 GiB |       1.62 GiB |           2.06 GiB |
|             89 |        1.45 GiB |       2.01 GiB | 1.49 GiB (refused) |

The refusal is a step-budget failure, so its RSS measures an incomplete proof. Concrete-key simplification procedures certified 89 growing pairs with two million steps and peaked at 4.57 GiB. A separate experiment restarted `cbv` on concrete states for small comma subtrees. Lean reached 15.2 GiB and exited with status 137 on RBE. [Concrete-key experiment](https://app.buildbuddy.io/invocation/b892555f-9083-402a-abda-20208c9d8226) · [Subtree experiment](https://app.buildbuddy.io/invocation/985427a0-e539-4468-9f86-a330148ed914).

The ordinary splitter now uses a kernel-checked batch rule for wide nested writes. The rule checks the live receiver, prototype and fresh keys, then appends the descriptors in evaluation order. A failed guard evaluates the original sequence. With this rule the 89-entry public numeric enum passes both strict emission and disk checking at the default reduction budget. The relocation test compares all 178 own keys and descriptors under Node and exercises the existing-property and read-only fallbacks. [Public certification run](https://app.buildbuddy.io/invocation/13ae2038-c34f-4c70-b743-4fbfaf431f05) · [End-to-end test](https://app.buildbuddy.io/invocation/700fbdaf-d699-4e08-a53b-a6af6adbf259).

The public 89-entry proof peaked at 1.20 GiB after imports, 2.38 GiB after the numeric key conversions and 3.32 GiB after proving key uniqueness. The expression witness added little to the conversion peak. The certificate now tries `rfl` on each numeric conversion before `cbv` and proves distinctness with the checked tree-set predicate. For ordinary certification of the same synthetic case, peak process RSS fell from 3.29 GiB to 1.87 GiB and elapsed time fell from 39.61 s to 24.24 s. The disk check also certified at the default reduction budget. [Proof-stage profile](https://app.buildbuddy.io/invocation/e4d7c826-07f8-41ca-8fc0-2bd712cb394c) · [Before](https://app.buildbuddy.io/invocation/ce5b7feb-f797-4d91-a0bb-ae2e9a789fbe) · [After](https://app.buildbuddy.io/invocation/da3dd012-6fdd-4612-a538-6281f8b01586).

The pinned 89-entry enum now certifies linked `normal-completion` through the ordinary splitter and `--check-output` in the 2 GiB replay sandbox. Node observes the same 178 own properties on the exported object in both programs. Changing one reverse mapping made the disk check reject the modified ESTree. Node independently observed the changed mapping.

**Three more language features on the pinned passing selections.** Static record fields now use the ordinary linked expression lowerer, identifier assignment supports local and module bindings, and unary `void` preserves operand evaluation. Together these raise isolated lowering from 3,675 to 4,121 of 8,082 selected units and from 4,667 to 5,200 of 9,707 overall. Every previously lowered unit still lowers. [Record values](https://github.com/agentydragon/ducktape/pull/19278) · [Identifier writes](https://github.com/agentydragon/ducktape/pull/19279) · [Void operands](https://github.com/agentydragon/ducktape/pull/19281).

The ordinary splitter and emitted-file check both certified three complete authored owners: a command-registration function that builds two option records, a three-declaration setter/getter with a live binding, and a helper that assigns a local binding. Node matched the source and split. Additional certificates execute the registration function and service setter during initialization, so the proof covers those operations directly. Moving the setter alone would make it write through an import. The checker rejects that placement and the Node preview throws on the call. A fourth complete owner uses `void 0` in a predicate. Its extraction includes a synthetic initializer that calls it on three inputs, passes both certificate checks and matches Node. The replay inputs and authored names live in the private repository or the pinned replay artifacts.

**Numeric string operands.** A one-call public initializer subtracting from a padded numeric string exhausted the default two-million-heartbeat budget after 104.90 seconds. The minimized `Num.ofString " 7 "` proof stopped at opaque `Float.scaleB`. Packing the rounded significand and exponent directly into binary64 lets Lean compute the conversion. The same initializer now certifies in 6.80 seconds at the same budget. A regression also checks an eager position predicate and a reversal that moves its input write after the call. Both orders finish, and the checker rejects the changed result that Node observes in the preview. [Before](https://app.buildbuddy.io/invocation/794a809d-700b-4268-80a8-7496a36b2b85) · [After](https://app.buildbuddy.io/invocation/2064b7bb-360b-428c-8d8c-c022a4b25d89) · [Linked regression](https://app.buildbuddy.io/invocation/49ce8621-78f2-41c8-9890-31895822bc26).

The pinned one-call and four-call excerpts now certify linked `normal-completion` through ordinary emission and `--check-output` with the same sources and placements. Node matches the single Boolean and all four classification results, including strict index comparison and nonnumeric string inputs.

**Observable host state at the first selected move.** The ordinary checker refuses the isolated initializer at its first host-global read. Under Node, the original and the forced split both finish normally and write the same payload. The property key comes from an error stack and contains the initializer's module URL. The key changes even with identical initializer bytes and the same entry URL. An extracted vendor consumer enumerates these keys, parses them, and attaches the payload to a frame identified by filename. With identical input frames and a controlled parser, changing the placement changes which frame receives it. This is a runtime counterexample for the isolated move under raw stack observations. A stable `typeof` contract alone cannot justify it.

A two-unit control kept that initializer in the entry and moved an independent selected constant. At the original entry URL, the vendor consumer produced the same filename-indexed event, but the generated import moved the stack frame down one line and the raw dictionary keys still differed. Preserving the owner and filename alone is insufficient for exact host state.

**Class proof cost.** The first eager subclass proof exhausted the proof worker's memory. Even removing all members and exporting a constant left `class Base {} const Child = class extends Base {};` above a 40-second proof budget with the superclass in its own module. For a class constructor with an own data `prototype`, the checked evaluation rule now reuses `classParents_ownPrototype` to resolve the two prototype links. Other superclass shapes use the original lookup. The [linked class fixtures](translation/linked/classes_test.mjs) exercise named and null heritage, base constructors, fields and accessors. [Bounded proof comparison](https://app.buildbuddy.io/invocation/d94476fc-498a-4716-bea2-2d3e144348cb)

**Acceptance priorities.** Replay the named spec on the pinned bundle with current Rust. Record the engine revision and the outcome for each requested pull, including selector or build failures. For refused pulls, minimize the dependency-closed source and the requested placement while retaining the scheduling change that triggered the refusal. If two pulls pass individually and fail together, keep the pair as the case under review.

- If the original and candidate differ observably under the intended contract, retain the counterexample and revise the placement or leave the affected units together. The stack-derived key in the host replay above is one such counterexample.
- If the placement can be justified, identify the missing inference in the checker that refused it. Add the rule with its proof where applicable, a positive replay and a nearby unsafe control. Agreement on sampled Node or browser inputs alone leaves the proof obligation open.
- If the evidence is inconclusive, record the missing observation, semantic assumption or proof. Keep the pull unresolved until that question is answered.

For emitted candidates, retain the applied graph, JS and entry exports. Run the ordinary Lean disk check and browser smoke test. If Rust emits different module glue from the ASTs Lean derives, track that mismatch separately from the semantic status of the requested move.

Keep the stack-sensitive move as a negative control for exact host-state equivalence. [Chromium regression](https://github.com/agentydragon/ducktape/pull/19261). A location-preserving candidate must retain line and column as well as module URL, and reproduce the complete observed key and consumer behavior. Normalizing locations would require an explicit, separately reported observation contract covering the consumer's reads. Continue certifying other exact dependency-closed selections in parallel.

### Known blockers on the production input

| Stage                         | Evidence and implication                                                                                                                                                                                                                                                                                                                                            |
| ----------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Placement and entry interface | The archived occurrence pairing has two source-order inversions. A scratch reassignment makes the schedule representable. The archived entry independently exposes 266 extra names, violating the exact-export contract. See [private production replay](#private-production-replay).                                                                               |
| Linked lowering               | In the earlier canonical survey, of the 6,941 units that refused, the most frequent first diagnostics are function declarations (3,138), arrays (715), expected identifier (704), complex object property values (691), member access (544) and class declarations (437). Lowering stops at the first error in each unit, so accepting one form can expose another. |
| Host semantics                | The [browser-page profile](#host-contract) supports the first ambient `typeof window` comparison and the bare page reference. The page/global fallback lowers; the initializer reaches construction of the opaque page Error.                                                                                                                                       |
| Reordering proof              | Moving an initializer across an opaque call or callback requires accounting for the state that code can reach and change. Use the minimized crossing to identify the specific proof obligation before widening a move rule.                                                                                                                                         |

Excalidraw remains the [public intermediate milestone](#excalidraw-public-intermediate-milestone). Keep private selectors, source and replay artifacts in the private repository.

## Picking this up

- **Build and test.** Use the [Bazel commands](README.md); CI runs `bbr test //devinfra/js/debundle/x/split_proof/...`. By hand, in `lean/`: `nix shell nixpkgs#lean4 nixpkgs#gcc --command lake build EsmSplit render Experiments lower lowersplit evalorder translate corejs` (about 2 minutes clean). The golden cases: `node ../effects/run.mjs` in `translation` (90 s). The model: `node differential.mjs --count 40 --seed 1` in `model` (20 s). Each JavaScript tool package’s `package.json.in` is copied to `package.json` before `npm ci`; never commit a `package.json`.
- **Generated coverage for supported features.** When adding a language feature or a proved move rule, teach a program generator to compose it with varied operands or surrounding statements. Include a CI seed sample that executes it through the relevant checker or interpreter and asserts the observed behavior. For a move rule, require a reordered plan that uses the rule and a dependency-bearing refusal. Track remaining gaps in the [CoreJS differential inventory](model/README.md) and the [translation coverage audit](translation/COVERAGE.md).
- **After any change to `Effects.lean`**: the golden cases, `node test.mjs`, `node fuzz.mjs --count 150 --seed <new>`, `footprint_oracle_sweep.mjs --samples 40 --seed <new>`, `debundle_cases` (its committed results must be regenerated if they change), and `fuzz_mutate.py` (about an hour; say so if it is skipped). Then measure the large bundle: the count of units that touch the world must be reported with the change.
- **The private evidence** is in the private repository `gaffer-private`, under the large bundle's `debug/translation-validation/`: `peel-probe/` (what peels, the bundle-order split, the browser load, the counts after the rule fixes), `startup-coverage/` (what runs at start-up) and the host inventory under `devinfra/debundle/host_inventory/`. Its scripts rebuild the inputs: the statement dump comes from [`translation/dump_ast.mjs`](translation/dump_ast.mjs) on the chunk, the footprints from `lower <dump> --premises <file>` (3 minutes, 1.5 GB), the partition from `scripts/peel_all.mjs`. The scratch copies of these inputs did not outlive the session that made them.
- **What reaches this repository from there** is counts and names that are public already. No spec names, no minified identifiers, no application names. Fixtures use neutral names.
- **Independent attempts pay.** Half an hour of a fresh agent told to break the analysis found dozens of wrong accepts each time. Repeat it after every change to the rules, and give the agent the generators and the oracle.
- **Review with programs the author did not write.** Every layer of the model was checked that way before it merged, on a seed its author had not used.

## Target

`lowersplit` has one guarantee and writes no unchecked output. `split` is total Lean code over CoreJS, and its type carries the theorem:

```lean
split X S = .ok Y →
  Places S X Y ∧                          -- each source unit appears once, in the module S names
  SameExports X Y ∧                       -- same entry names, bound to the same live cells
  NormalEquivalent (Worlds premises) X Y  -- same successful final states, in both directions
```

`X` is the lowering of the original module graph. `Y` is the lowering of the printed output. [`NormalEquivalent`](lean/EsmSplit/CoreJS/LinkedPivot.lean) and [`SameExports`](lean/EsmSplit/CoreJS/LinkedBehavior.lean) exist; `Places` does not yet. `Worlds premises` are the worlds that satisfy the host premises the verdict lists. When `Y` runs the units in the original order, `split` also proves that every ending matches, including throws.

The planner proposes a candidate and may use unproved heuristics. The checker inside `split` decides. A planner bug can cause a refusal, never a wrong accept.

Moves are compared only on runs that end normally. Without that restriction the statement is false: in `const o = { a: undefined }; console.log("printed"); const w = o.a.b;` the throwing read can move ahead of the log ([`experiments/README.md`](experiments/README.md) § 14). The comparison holds in both directions, so one program ends normally exactly when the other does. A program that prints and then runs forever is not compared, because the model keeps no output from an unfinished run.

### Trusted and tested

The theorem starts and ends at CoreJS. These parts connect it to JavaScript. They are tested, not proved:

- Acorn's parse and eslint-scope's annotations.
- One lowering from annotated ESTree to CoreJS, written in Lean. It lowers the original graph and the reparsed output.
- CoreJS semantics and the world model for the supported fragment, including the host function table (`worldFunction` in [`Value.lean`](lean/EsmSplit/CoreJS/Value.lean)).
- Lean's kernel, plus its compiler when a proven checker runs as compiled code.

The Node comparison suites ([`model/README.md`](model/README.md)) exercise the same lowering and interpreter as certification. The emitter and printer are not trusted: publication reparses the printed files and checks that they lower to `Y`.

### Decided

- **One lowering, in Lean.** The same Lean code lowers annotated ESTree for certification, for the `corejs` interpreter and for the Node comparison suites.
- **One guarantee.** Callers do not choose a proof technique or level. The verdict reports the evidence and premises it used.
- **No unchecked output.** Every written module graph is certified. Diagnostics report and write no modules.
- **The planner stays unproved.** Heuristics such as `Effects.lean` propose candidates. Their output counts as evidence only after a proved check accepts it ([footprint soundness](docs/footprint_soundness.md)).
- **A proved rule for a narrow fragment is worth landing.** It widens what the one checker accepts.
- **No new precision rule goes into the unproved footprint analysis** unless a measurement shows it is blocking. Fixing a wrong rule is always in scope, and its cost on the large bundle is measured before it is adopted.
- **The world is a parameter.** Real bundles reach unknown code: without P3, 1,751 of the large bundle's 9,707 units touch the world. The theorem holds for every world that satisfies the listed premises. A world reads and changes only what it is given.
- **The standard library is part of the world.** Each standard function gets a stated class of behaviour, such as "a function of its primitive arguments", "reads its receiver" or "may call its argument". That table is a tested assumption about the engine ([`docs/lessons_learned.md`](docs/lessons_learned.md) § The first slice of a model).
- **What is refused** ([`SUPPORT.md`](SUPPORT.md)): a top-level `await`, a top-level `using`, a direct `eval` anywhere, a name declared twice in one list. A `Proxy` is never covered by premise P3.
- **Premise P3 is not assumed for real inputs** (finding F7, 2026-11-21). Code of the program that the world runs can add a getter or a `toString` to an object the world holds, and the Claude bundles do it. Without P3 the analysis is sound for that, and the cost is small: 3 to 6% more units touch the world on three production bundles, and peeling barely moves ([`generator_findings`](translation/debug/generator_findings/README.md)). The cases and fuzzers still set P3, to test the analysis under it.

### Open decisions for the owner

- How the four input files with a direct `eval("require")` are admitted: a check of the string, or an audited premise.
- Whether a reviewed vendor table is worth its review cost. Alone it does not change which modules peel.
- Whether debundle's Rust gate stays as an extraction diagnostic once Lean certifies the production split. Readable names move into the checked split ([renames](#renames-inside-the-checked-split)); until then debundle emits its tree unproved and `translate` referees it.
- Whether the vendor chunk becomes part of the program in the model. It removes most unknown code, and the model then has to run what the vendor code runs at start-up, regular expressions included.

## Convergence

The code reaches the [target](#target) through the steps below. Until they land, new work does not add a lowering, a proof mode or an uncertified output.

[Check the printed files](#check-the-printed-files), [no unchecked output](#no-unchecked-output) and [documentation](#documentation-states-what-each-verdict-proves) are small and independent. [One lowering](#one-lowering-in-lean) comes before [one proven checker](#one-proven-checker), because the theorem is stated over that lowering's output. [One guarantee](#one-guarantee) can remove the mode flags before the proven checker exists by picking a strategy automatically.

### One lowering, in Lean

**Now.** Four code paths turn JavaScript into CoreJS:

| Path                                                                                                                              | Used by                                                                                                                                                                               |
| --------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [`model/to_core.mjs`](model/to_core.mjs), read by [`Decode.lean`](lean/EsmSplit/CoreJS/Decode.lean)                               | Node comparison suites (`corejs programs.jsonl`), `corejs --footprints` and `--moves`, `lowersplit --diagnostic --corejs`, [`translation/split_dump.mjs`](translation/split_dump.mjs) |
| Basic [`FromESTree`](lean/EsmSplit/CoreJS/FromESTree.lean) rules (`expr`, `declaration`, `unit`, `program`)                       | `corejs --ast`, closed-declaration certificates, `--diagnostic`                                                                                                                       |
| Linked `FromESTree` rules (`linkedUnit`, `linkedDeclaration`) through [`LinkedInput.lean`](lean/EsmSplit/CoreJS/LinkedInput.lean) | Linked certificates, structural pivot units                                                                                                                                           |
| [`OpaqueInput.lean`](lean/EsmSplit/CoreJS/OpaqueInput.lean)                                                                       | Structural units kept opaque                                                                                                                                                          |

The broad Node comparison runs through `to_core.mjs`. [`corejs_ast_lowering_test.mjs`](translation/corejs_ast_lowering_test.mjs) compares the basic Lean rules with `to_core.mjs` and Node. No test runs the linked rules and the interpreter against Node on a single program; the linked tests run accepted splits in Node. `to_core.mjs` also decides which assignment targets are imports and which names are host globals ([debts](#debts)).

**Target.** `LinkedInput` with the linked `FromESTree` rules is the only lowering. Keeping a unit opaque is one of its results, chosen per unit, with captures checked the same way in both graphs.

**How.**

1. Make `corejs --ast` lower module graphs through `LinkedInput`.
2. Run every Node comparison suite through it: hand-written, module and opaque cases, generated programs, the binding and inheritance suites and the sweep. A model mismatch then reproduces on one program. An unsupported program is a reported refusal, never a fallback to `to_core.mjs`.
3. List the programs only `to_core.mjs` accepts. Port each feature to the linked rules, or record it as refused.
4. Before moving opaque fallback, check which cells a deferred body captures, its part number, and the handling of direct `eval`, module identity and top-level `await`. Replay Excalidraw on the AST alone.
5. Merge `OpaqueInput` into the same entry point.
6. Delete `to_core.mjs`, the program decoder in `Decode.lean` (the world-recording decoders stay), `--corejs` and the basic `FromESTree` rules.

**Done when.** One Lean function lowers an annotated ESTree module graph to CoreJS, CI runs the Node comparison suites through it, and nothing imports `to_core.mjs`.

### One guarantee

**Now.** Certified verdicts have three scopes, and each proves a different statement:

| Scope                 | Selected by                                                                       | Proves                                                                           |
| --------------------- | --------------------------------------------------------------------------------- | -------------------------------------------------------------------------------- |
| `closed-declarations` | Automatically, for a closed module of simple declarations, without `--structural` | Order kept: every ending matches. Moves: same successful states, both directions |
| `linked-programs`     | Default                                                                           | In every allowed world, both programs finish normally in the same state          |
| `linked-structural-*` | `--structural`                                                                    | Same successful final states, both directions (`NormalEquivalent`)               |

`--require all-outcomes|normal-completion` asks for a level.

**Target.** Every certified verdict states the target theorem. The checker picks the strategy. The verdict reports the evidence used, its premises and, when the order is kept, that every ending matches.

**How.**

1. Prove that linked `EquivalentFor` implies `NormalEquivalent` over the same worlds. It should follow from determinism of `Ends`.
2. Report every scope under the one guarantee.
3. Pick the strategy automatically. Remove `--structural` and `--require`.
4. Run the closed-declaration tests through the remaining checker. Retire that path when they certify, or record what is missing. [Declaration certificate](lean/EsmSplit/CoreJS/Declaration/Certificate.lean)

**Done when.** No CLI flag chooses a proof technique or level, and the closed-declaration path is gone.

### One proven checker

**Now.** [`checkStructuralSchedule`](lean/EsmSplit/CoreJS/LinkedPivotCertificate.lean) carries a general theorem in its type and runs as compiled code. Linked mode has no general checker: [`LinkedCertificate.lean`](lean/EsmSplit/CoreJS/LinkedCertificate.lean) writes a new Lean proof for each run, and a separate Lean process checks it. No Lean function has the target theorem in its type. `checkLayout` and the lowering enforce placement outside the proved statement.

**Target.** The total `split` from the [target](#target). The planner runs inside it and stays unproved.

**How.**

1. Wrap planning and `checkStructuralSchedule` in `split`. The CLI emits ESTree for `Y`'s placement, and [checking the printed files](#check-the-printed-files) ties the files to `Y`.
2. Define `Places` and prove that the placement check establishes it.
3. Add whole-program evaluation to the checker as a proven justification: a symbolic evaluator with a soundness theorem, so a move can rest on evaluating the units it crosses, as linked mode does today.
4. Retire per-run proof generation once the proven evaluator certifies the linked test corpus.
5. Optionally let the kernel replay the checker's decision on small inputs. That run then does not trust the compiler.

**Done when.** The certified CLI calls only `split`, `split`'s type states the target theorem, and no run writes a proof module.

### Check the printed files

**Now.** [`SplitFiles.lean`](lean/EsmSplit/SplitFiles.lean) certifies the planned ESTree, prints it and renames the staged directory into place without reparsing. `--check-output` reparses only on request and compares ASTs with scope annotations removed.

**Target.** Before publishing, reparse the staged files and lower them with the one lowering. The result must equal `Y`. The emitter and printer leave the trusted parts. `--check-output` runs the same check on an existing directory.

**How.**

1. After printing into the staging directory, reparse and lower every module and compare the result with `Y`.
2. Make `--check-output` call the same comparison.

**Done when.** A printer change that drops required parentheses is refused at emission.

### No unchecked output

**Now.** `lowersplit --diagnostic` writes uncertified previews. The Node comparison suites send split plans through diagnostic mode ([`model/README.md`](model/README.md)).

**Target.** Diagnostic mode reports plans, refusals and footprints and writes no module files.

**How.**

1. Move the split checks of the Node comparison suites to the certified path. A plan it refuses is counted as a refusal; its program still runs unsplit through the model.
2. Remove module output from `--diagnostic`. Plan reports, `--lean` and `--move-report` stay.

**Done when.** `--diagnostic` takes no output directory.

### Renames inside the checked split

**Now.** [`translate`](translation/README.md) validates a separately authored tree under the premise that renaming preserves meaning.

**Target.** The spec carries renames, and `split` checks them, including code that observes function names or source text.

**Done when.** A certified split emits readable names without a renaming premise.

### Documentation states what each verdict proves

**Now.** The README's "What is proved" section is about `split_correct`, a theorem over abstract statement meanings; the verdicts state CoreJS theorems. The README says the theorem ends at the ASTs, but the AST-to-CoreJS step is executable Lean with `partial` helpers that the kernel cannot unfold, and closed-declaration and structural verdicts come from proven checkers run as compiled code. [`docs/footprint_soundness.md`](docs/footprint_soundness.md) cites `LowerSplit.sliceLines`, which no longer exists.

**Target.** The README states the target guarantee, the [trusted parts](#trusted-and-tested) and which evidence each verdict used. Until [one guarantee](#one-guarantee) lands, it states each scope's theorem.

**Done when.** The README describes exactly the verdicts the CLI emits.

## Next steps

Use the production milestone above to prioritize the proof and engineering tracks below. Record the checks and measurements for each completed step in its PR.

### Private production replay

On 2026-11-22 the pairing covered all 9,707 body units in the archived review tree. The archived pairing assigns three occurrences of the same call to output positions that put two owners out of source order. Reassigning those occurrences in a scratch pairing preserved every output position and let the current adapter represent the full body-unit schedule. The calls have identical source syntax and resolve to the same callee. This checks placement representability only. Independently, the reviewed entry exposes all 268 original export names and 266 extra names. Recheck both the schedule and the public interface before treating this tree as a certification candidate.

### Host contract

The ordinary splitter reads `{"profile":"browser-page"}` from `--host-contract`. Both module trees lower page aliases and guarded host detection to explicit CoreJS observations. Lean proves equivalence for every world where the observations used return the specified value without changing host state. A bare read carries the page object as an opaque reference. Lexical names use their actual cells. The linked verdict reports the selected profile and its execution premises. A public reversal fixture has equal results in a page and different results in a realm without `window`. [Profile theorem and end-to-end tests](https://github.com/agentydragon/ducktape/pull/19333) · [Conditional reversal](https://github.com/agentydragon/ducktape/pull/19336).

The captured page directly loads the pinned chunk as a module. Its exact first `"undefined" != typeof window` comparison certifies at linked normal completion on emission and from disk with the page profile. The unprofiled comparison refuses on `window`. The whole initializer lowers the guarded `global`, `globalThis`, and `self` alternatives. It reads `Error` from the page object and reaches opaque construction during evaluation. Its stack-derived registry writes remain observable. The host replay includes a document observer. Running the isolated comparison in page-less Node returned `false` in both trees.

A profile can also name opaque constructors in deferred code. If initialization reaches a lookup for one of these constructors, CoreJS reports an unknown result before evaluating the arguments, even inside `try/catch`. A successful extraction that merely carries a deferred `new Error(...)` can therefore report an empty list of execution premises. A paired survey found no complete owner whose remaining host operations were all `typeof`, and six canonical, wholly selected owners recovered by lowering deferred error construction. Preserve the distinction between retaining syntax, certifying complete initialization, and observing calls to exported functions afterward.

**Next host inputs.** Add a Node.js or worker profile when it unblocks a measured extraction. The author should be able to extend a named profile with typed premises, for example a stable effect-free `typeof appConfig` or an explicitly absent binding. These extensions are planned. Reject contradictory entries and report which premises the theorem requires. Add an absent-versus-object comparison in matching runtimes when both profiles exist.

**Realm intrinsics.** Use the same authored host contract for the ambient `Error` binding. A host profile can identify the binding as its realm's standard error constructor. Lean should supply the reusable semantics for each supported operation. Resolve lexical bindings first. For `class E extends Error {}`, creation observes the value of the binding, its constructibility, and the observable lookup of its `prototype` at that point in initialization. Code can replace the global binding. Both schedules must satisfy any stability hypothesis, including across intervening calls or writes. An eager `new E(message)` additionally requires the semantics of superclass construction with the derived `new.target`. Reading `stack` needs separate host-specific behavior. Record the premises actually used in the certificate. Cover a shadowed binding and a reordered reassignment that makes the subclass inherit from a different constructor.

### Excalidraw (public intermediate milestone)

For the pinned public bundle, deliver a spec whose emitted modules Lean can certify and whose ownership follows the [upstream Excalidraw sources](https://github.com/excalidraw/excalidraw/tree/v0.18.1/packages) as closely as the evidence supports. Use upstream files and readable member names to justify placements. Leave unresolved units in the original entry and record what prevents them from moving. The [integration README](integration/excalidraw/README.md#certified-placement) distinguishes the checked-in browser baseline, certified proposals and the full authored replay.

Grow the spec one upstream dependency group at a time. Resolve its selectors against the pinned input, review the ownership and import-priority diff, then certify the **combined** placement at emission and from disk under the stated host contract. Exercise the certified overlay in Chromium against the original app. Record which upstream files and members were recovered and any remaining deviations. A refusal applies to the tested placement and order; retain its witness so the next trial can distinguish a changed export or observed behavior from a limitation of the checker.

The next editor decision is whether to adopt the [scroll dependency proposal](integration/excalidraw/proposals/scroll/README.md) and its explicit import priority in the maintained spec. It certifies with the recorded order; the authored priority was refused for the same owners. For the remaining `calculateScrollCenter` dependencies, the proposal records both the private exports that a partial move introduces and the first refused moves in the larger closure. For the main entry, the [renderer trial](integration/excalidraw/main.md) isolates a retained module metadata reader. Keep the [ordinary scroll checker trials](integration/excalidraw/README.md#strict-certification-of-the-scroll-module) as regressions for that proof mode.

### Proof track

**N4. Widen the slice that moves code.** `moves_preserve` is proved; what `MayMove` covers is 69% of the large bundle's units.

The active [footprint soundness work](docs/footprint_soundness.md) establishes a compositional contract for checked Effects output, starting with binding-only statements and keeping unsupported analysis rules outside the proved slice.

- _Opaque-code frame proof:_ [`OpaqueFrame.lean`](lean/EsmSplit/CoreJS/OpaqueFrame.lean) proves conversation framing from per-request conditions and commutation with allocation-only declarations in both directions. [`Reachability.lean`](lean/EsmSplit/CoreJS/Reachability.lean) now defines distinct object and binding nodes, rooted in handed objects and supplied bindings, following binding values, properties, accessors, prototypes and closure captures (including `this` and opaque captured cells). A closed envelope contains this least reachable set and excludes the protected region and initializer read bindings. [`Request/Frame.lean`](lean/EsmSplit/CoreJS/Request/Frame.lean) derives framing for the actual binding read, initialization, assignment and own-key requests from that invariant. It preserves the envelope on normal, thrown and unknown outcomes, including handing out an object and publishing an alias through another binding. When every request the world can issue preserves one fixed envelope, the full conversation and the world-backed statement preserve it too. A caught request exception can pass an accessible object handle back to the world before the next turn. Kernel-checked examples cover separated mutation, caught TDZ errors, immutable writes, forged handles and hidden captures.
- [Object allocation requests](lean/EsmSplit/CoreJS/Request/Allocation.lean) preserve the protected region and read bindings. The reachable-object envelope can grow by the newly allocated address. An explicit prototype comes from a supplied handle. Default construction requires the intrinsic Object.prototype in the envelope. A callback captures the supplied binding cells and inherits from Function.prototype. A constructible function also allocates an own prototype inheriting from Object.prototype. The envelope grows by the two fresh addresses and contains both sides of the constructor/prototype cycle. In the Excalidraw editor, unit 99 names only `Uint32Array` and `f6`, and the closure initialized at 98 has no free names. The frame for unit 99 must also cover callbacks and handles already handed to the world.
- _Prototype lookup:_ [`Request/Property.lean`](lean/EsmSplit/CoreJS/Request/Property.lean) frames prototype search even when replacing the protected region changes the total object count. The first matching property is on a path of distinct objects in the envelope, so either heap has enough search fuel. The actual `in` request uses this proof, and a property read uses it when getter calls also preserve the frame.
- _Next for opaque code:_ prove invoking a callback preserves the invariant, add roots for other operations that introduce built-in objects, and handle property writes that can invoke setters. Then establish the invariant after linking and through the prefixes used by reordering. The restricted request theorem does not certify a whole arbitrary opaque conversation, and `MayMove` is unchanged. Its current `Commute` theorem quantifies over arbitrary states: an opaque statement can call a previously handed closure that reads a moved binding absent from its direct free names. A footprint/name-disjointness test alone does not establish separation. Measure newly certified whole modules and failing pairs when a proved condition reaches the verdict.
- _Needs:_ classes in `Expr.allocOnly` (`allocOnly_keeps` and `allocOnly_local` for `evalMembers` and the static field initializers): 421 opaque units, most of them blocked by a standard global (251) or `extends` (162).
- _Measured_ (2026-11-21, the verdict's moves case): the 886-module split runs 5,420,546 pairs out of order and 134,859 fail `MayMove`; the bundle-order split of 2,364 runs 2,571,190 and 1,395 fail. A split built with `MayMove` as one more conflict keeps 2,368 modules whole and the verdict names the moves slice for it.
- _What blocks the reviewed tree_ (2026-11-21, against the tree's own module order; scripts in gaffer-private `certified-split/`): 1,961,504 of its 7,611,199 out-of-order pairs fail `MayMove`.
  - About 1,110,000 pair a statement that only allocates with an opaque one whose footprint never reaches its names. A frame lemma for opaque code (a unit the world runs commutes with an allocation it cannot reach) covers them. The count is gated on footprints, not proved. On the bundle-order split it covers 889 of the 1,395.
  - 141,696 reorder two statements that the footprint analysis calls effects. No `MayMove` rule covers these. The world calls commute, or the spec is wrong: decide case by case.
  - Of the 718,365 failing pairs where neither statement is an effect, greedy by pairs resolved: classes 213,764; calls of bundle functions and IIFEs +119,877; pure expressions (operators, property reads, templates) +282,425; standard built-ins (`Map`, `Set`, `RegExp`, `Object.*`) +101,547.
  - On the bundle-order split, by reviewed modules freed: classes 15, built-ins +23, pure expressions +27, calls of bundle functions +57, the world +3 (125 in all).
- _Done when:_ the share is near the census's 73%, or the shortfall is explained by counted statement kinds.

**N5. Remove the imports that exist only for order.** The bundle-order split of the large bundle has 2,744 bare imports in 629 modules, nested 466 deep. Each is a candidate to drop, and dropping one is a claim that two modules commute. First candidates: a forward mention of a module that only declares functions. The count of ordering imports going down is the measure of how readable the output is. The second rule to prove belongs here: unknown code cannot write a name declared later, except through an object that escaped earlier. The peel probe measured it as the largest lever (873 modules that peel alone would become about 1,624, an optimistic count).

### Engineering track

**E2. `import.meta`.** `translate` rejects a unit that reads it, except `new URL("/absolute", import.meta.url)`; 71 input files read it at the top level. What moving code changes is `import.meta.url` and what is resolved against it. Supporting it means rewriting or pinning the base when a unit moves to a module in another directory. Done when the fixtures `meta_relative` and `meta_absolute` have accepted counterparts that Node agrees with, and the count of input files refused for it is reported.

**E3. The language features that keep units out of the model**, ranked by how many of the large bundle's 5,636 opaque units each one blocks, counting every feature a unit holds, in function bodies too (the first number), and the units it alone blocks (the second). The rank is static presence, not what runs at start-up: a statement is opaque for a feature anywhere in its text.

- Imported names as bindings of other modules (cross-chunk imports): 2,832 / 1,194. Alone it takes the units in the model's syntax from 42% to 54%.
- Optional chains: 1,739 / 218. Destructuring or default parameters: 1,641 / 159. Standard globals beyond the host table (`Object`, `Map`, `Error`, …): 1,443 / 367. Destructuring elsewhere: 1,160 / 64. `async` and generators: 1,028 / 65. Spread: 939 / 42. `for`-`of`: 842 / 59. With these eight, 86% of the units are in the model's syntax.
- Late: browser globals (727 / 107), regular expressions and BigInt (187 / 81), `class … extends` (162 / 68), `switch` (150 / 21), `for` with several declarations (66 / 66). `extends` needs the prototype chain of constructors, `super(…)` and `super.m()`, and the dead zone of `this` before `super` returns.
- For the slice that moves code these matter less than classes: 1,333 of the opaque units that are left are declarations that call a function, and no feature makes those movable under `MayMove`.

**E4. The mutants that survive.** `fuzz_mutate.py` leaves 7 of 155. `no Hygienic` is known, and `nested hooks are unbounded` is harmless since the hook depth is counted. Five survived before the last fixes too and were never looked into: `a replayed call drops what it threw`, `memoized calls replay no writes`, `an unseen constructor does not escape`, `Intl reads no options`, `Error reads no options`. For each: a case that kills it, or the reason it is equivalent. The harness was not run after the `throw` rule; run it first.

**E5. Evidence that went stale.** After the rule fixes and the `throw` rule only the counts of units and of whole modules were measured again. Not measured again: the order obligations of the reviewed tree (670,997 before), the census of single peels (873), the vendor ladder, and the browser load of the accepted split (3,052 files now). The lessons file says which numbers are from before.

**E6. Library code.** `lower` aborted on the 20,032-unit vendor chunk with a stack overflow. The cause of F5 (a hook that reaches itself) fits that symptom better than deep syntax, and F5 is fixed: run the chunk again before anything else is built for it.

## Debts

What is owed beyond the steps above. Each is a known gap, not a plan.

**In the model and its proofs**

- `Program.evalOrder` is the same function as `Plan.visit` of the proved checker: tested on every program of the model's test, not proved.
- The table of 88 host functions (`worldFunction` in [`Value.lean`](lean/EsmSplit/CoreJS/Value.lean)) is an assumption about the engine: called on primitives, each returns a primitive or throws and reads nothing the program can change. It was checked from memory of the specification, not against its text.
- A world with one state orders every pair of host calls. The model cannot yet say that `Math.abs` does not read or change the world, which a split that moves code needs.
- Read-only imports rest on the front end: [`to_core.mjs`](model/to_core.mjs) decides from the text which assignment targets are imports (`Target.frozen`).
- The front end is trusted and is JavaScript: it decides what a host global is by asking the Node that runs it, and which declaration a name means.
- Not in the model: `**` (the specification leaves it to the implementation, and the C library's result differed from V8's in the last bit), `instanceof`, conversion of an object to a primitive (`valueOf`, `toString`), writes to arrays, namespace imports, and source-bearing re-exports.
- A run that ends out of fuel keeps no output, so a program that prints and then runs forever is not compared.
- The generated programs barely reach the model: 17% of their units run. The hand-written cases carry the object model.
- `Meaning` in [`Semantics.lean`](lean/EsmSplit/Semantics.lean) has no notion of a statement that does not finish, so the abstract theorem cannot say that a split ends normally exactly when its bundle does. The CoreJS verdicts state it.
- `ScopesAgree` has no instance proved in Lean, so `order_kept_preserves` has no worked example inside Lean. The evidence that its hypotheses hold for real splits is the test's comparison of final states, which is by a hash of a text of the state: the name, the parameters and the kept bindings of each function, with the bodies compared apart as tokens.
- `AssignsHome` has to hold for `splitProgram` to be what the front end reads from the files the tool writes, and it is not a hypothesis in Lean. The test checks per statement that the tokens are the bundle's.
- Only 19 to 28 of the accepted splits of a run of the four-way test run statements in another order than the bundle, so that test says little about reordering.
- The recorder of host calls takes a call for the program's when a stack frame's file is under the test's work directory. A change in how Node reports frames breaks it loudly, by a replay that does not match.
- [`Scope.lean`](lean/EsmSplit/CoreJS/Scope.lean) needs a raised heartbeat limit for the expression case and takes about 35 s to check.

**In the footprint analysis**

- It is `partial` code over JSON, so nothing about it can be proved as it is.
- Premise P4: an exception that the language raises itself in pure code is not ordered. `lowersplit` accepts a split that moves `const w = o.a.b` across a `console.log` when `o.a` is `undefined` (a named case of the model's test).
- The `throw` rule does not follow which branch is taken (`if (n > 5) throw …` counts when `n` is 1). Calls under a `try` are remembered apart from the same call outside one, and no test tells that from a shared memory: three attempts to make a caught call be replayed for an uncaught one failed, because a run that grows the heap is not remembered.
- Three precise rules went in where the fail-safe one cost too much: `Array(n)` is an array, iterating a collection yields pairs, and the `prototype` of a class is not the world's. Each has a case and a mutant.
- `catch` binds the world's value only when the unit ran code the analysis did not see. This rests on every skipped piece of code setting that flag.
- 55 of the 69 units that the fix of F6 made effects on the large bundle come from "every row converts its arguments". A flag per row would win them back.
- `lower` alone gives a unit with a direct `eval` a footprint without the names the string assigns; `lowersplit` and `translate` refuse such a bundle.
- A top-level `await` in a module that the bundle imports is not detected: the tools do not read those modules.
- The libraries (`lodash`, `acorn`, `semver`, `zod`) were not run through the footprint oracle after the fixes.
- The known gaps of premise P1 and of the general rules, five programs each ([`TODO.md`](TODO.md)).

**In the checker and the planner**

- The entry's public surface is not in `Valid`, and the rule that `import` and `export` statements stay in the entry is outside it (step 4 of the next section, and [`TODO.md`](TODO.md)).
- The order-keeping planner nests imports 466 deep on the large bundle. Node loaded a chain 3,000 deep and failed at 10,000.
- [`README.md`](README.md) § What is not proved lists the rest: the ES order model, hygiene, the printer, the lowering front end.

**In the evidence**

- The browser load checks start-up and the first render of a signed-out page with the network cut off. Nothing was logged in or used.
- The start-up coverage is one such load. A signed-in start runs more.
- The host inventory did not scan one vendor module that could not be rebuilt in the session that made it.

## More of debundle's cases through the Lean checker

Use the [saved corpus results](debundle_cases/results/summary.md) for the
historical baseline and the [adapter notes](debundle_cases/README.md#outcomes)
for verdict meanings. Regenerate the results to measure the current adapter.

Acceptance for every step, after running the [debundle case corpus](debundle_cases/README.md#run-it):

- `unsound-accept` stays at 0. A step that unlocks entries is also a soundness test: the first run of the corpus found two accepted splits that fail to load.
- The step's entries leave `unsupported` and `strict-*`, or the PR says why one stays.
- `lean:example_test`, `fuzz:render_fuzz`, and the mutation test still pass.

Steps are independent PRs. Steps 2–5 touch different Lean files and can run in parallel. Order by entries unlocked per effort:

### 1. Use Rust selector resolution in the corpus adapter

The Excalidraw browser smoke already uses Rust's `spec resolve-placement` output to bind the source digest, requested owners and authored import priority to one input. Lean computes and checks the resulting schedule. The [corpus adapter](debundle_cases/run_cases.mjs) still matches binding selectors and hole-free templates itself. The saved baseline has 54 comparable entries in this group and 12 refusal tests.

Reuse the resolver output for corpus entries with holes, alpha-renaming, ranges or dataflow selectors; keep the refusal tests distinct. Measure the cost of resolving one entry and check that the adapter maps resolved units to the same declarators before relying on the results. See the [selector semantics](../../docs/selector_resolution.md) and the remaining [production handoff work](TODO.md).

### 2. Declarator granularity in the adapter, and repeated `var` (6 comparable entries: 3 not run, 3 `strict-but-runs`; 2 more are refusal tests)

- [`dump_ast.mjs`](translation/dump_ast.mjs) writes the annotated ESTree Program. The [corpus adapter](debundle_cases/run_cases.mjs) uses `topLevelStatements` to enumerate one unit per `var`/`let`/`const` declarator from its parsed Program. A destructuring declarator stays whole (`comma_list/destructure_split_across_modules_is_rejected`). Let a template that matches a whole declaration list claim each of its declarators.
- `UniqueDefs` becomes "one module per name". Several `var` declarations of a name may sit in one module and keep their order, because they conflict. This changes `Semantics.lean`: the last declarer wins. `split_correct` needs a new proof.
- Entries: 2 of the 4 `statement claimed by two modules` (the other 2 are refusal tests), `sibling_declarator_ownership`, and the 3 `UniqueDefs` rejections.

### 3. Purity precision (3 of 5 `strict-but-runs` left)

[`Effects.lean`](lean/EsmSplit/Effects.lean) runs pure local calls and applies a table of standard-function summaries ([`effects/`](effects/README.md)); `initialization/frozen_callback` and `native_collections/duplicate_primitive_spread` pass now. Left:

- `emission/proxy_constructor/fresh_args`: `new Proxy` is a call into the world. A proxy runs its handler's traps, and an empty handler can gain one later, so a row needs a soundness argument ([`docs/purity_soundness.md`](../../docs/purity_soundness.md)).
- `callbacks/from_entries_map_does_not_run_stored_callback_during_construction`: the result is exported, and what is read from an exported object is the world's, since a module in a cycle may have changed it. A claim in the spec that no cyclic importer observes it would clear it.
- `initialization/frozen_record`: the declaration of `later` moves before the unit that gives `peek` to the world. Moving a declaration earlier is harmless when nothing in between can call `peek`; the model orders every reader and binder pair. A hint (step 2) says it.

Guard anything added with the corpus' rejections that must stay rejected (`proxy_constructor/effectful_proxy_keys`, `freeze_effectful_argument`, `freeze_proxy_receiver`), the fuzzer and the mutation test.

### 4. The entry's public surface

- `ExportsUnique` keeps export names distinct within each module. The certification step must also compare the entry’s public name-to-binding mapping with the original. An extra glue export changes what a namespace importer can observe.
- Reuse an existing public alias where possible. For example, if the entry already exports `X` as `av`, a generated importer can use `import { av as X }` without widening the entry’s interface.
- The same step can fold the entry-only rule for `import` and `export` statements into `Valid` (TODO.md).

### 5. Import-only edges for evaluation order (1 entry, likely more after step 1)

`mkPlan` adds a bare `import "./m.mjs"` when a statement reads a name at init time through its `reads` closure, so the declaring module runs first. `early_entry_importer…` needs it. The splitter is untrusted: `Valid` still checks the result.

### 6. Imports and several chunks (18 entries: 12 multi-chunk, 4 `ImportDeclaration`, 2 specifier selectors)

- `lowersplit` takes a chunk with imports from outside modules: the `import` statements stay in the entry, which writes them first, and the order of evaluation is argued, not proved ([`README.md`](README.md) § What is not proved). Model it: `import { x } from "…"` as a hoisted definition of `x` plus a foreign module that evaluates first, so that `OrderOk` covers the order and a module that awaits at its top level is refused or handled.
- The 4 `ImportDeclaration` entries need three things of the adapter: literal values in its dump (`lowersplit` reads an import's source from `lit`; `dump` writes it), the modules written beside the chunk, where its relative imports resolve, and `import` statements left in the entry when debundle's spec moves one. A probe with the first two: `return_object_provider_import` is accepted and prints the chunk's output, `chunk_renames/named_imports` agrees, `chunk_rename_peeled_import` is rejected for an `import` outside the entry, `pruned_import_keeps_its_load_order` fails `OrderOk`.
- Model several chunks as one bundle each, with import edges between them. `Plan.visit` and `OrderOk` run over the union.
- Oracle for the evaluation order: `es_order/check.mjs` (500 random import graphs against Node). Add cross-chunk graphs.
- Largest step. Do it last.

### 7. Audited premises (5 entries: 3 `annotations`, 2 `chunk_analysis_options`)

- Accept `pure` and `commuting` as explicit assumptions in the `lowersplit` spec: `effect := false`, or drop one conflict pair.
- The certificate prints them. The Lean theorem becomes conditional on them: `Valid B S P` holds under the assumption set. They are the author's claims, so Lean does not check them.

### Not planned

The 36 `reject: stderr` entries (7 run, 29 not run) test debundle's diagnostics (ambiguous selector, unknown anchor, parse errors, `import_order` contract). They have no counterpart in a validity check.

The 80 other entries that are not run today are covered by steps 1, 2, 6 and 7 (54 + 3 + 18 + 5). After those steps only the 36 are left, plus whatever new causes the unlocked entries show.
