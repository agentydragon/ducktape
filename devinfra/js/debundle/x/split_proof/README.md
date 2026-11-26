# Splitting a bundle into ES modules, with a Lean proof

A flat JavaScript bundle goes in. A spec says which top-level statement goes to which module.
The experimental splitter publishes ES modules only with the strict all-outcomes
CoreJS guarantee. The [target theorem](PLAN.md#target) covers every input and spec
accepted by the constructor, with its proof checked when building the splitter.
[Certification commands and current acceptance](#request-a-corejs-guarantee)

[`SUPPORT.md`](SUPPORT.md) inventories the syntax, standard library and host
features the intended inputs require. [CoreJS coverage](model/README.md) tracks
the fragment exercised by the model tests.

From the repository root, run the same checks as CI:

```sh
p=//devinfra/js/debundle/x/split_proof
bbr test "$p/..." --config=ci
```

Bazel supplies Lean, Node, Python, and the JavaScript dependencies. For focused checks and longer experiments, use the following commands with the same `p` (`out` is an absolute output path):

| Task                                                         | Command                                                                               |
| ------------------------------------------------------------ | ------------------------------------------------------------------------------------- |
| Compare the rendered example and rejected controls with Node | `bbr test "$p/lean:example_test"`                                                     |
| Render the verified example                                  | `bb run "$p/lean:render" -- "$out"`                                                   |
| Audit axioms and check the lowered example certificate       | `bbr test "$p/lean:axioms_test" "$p/lean:lowered_example_certificate_test"`           |
| Compile the extra Lean proofs                                | `bbr test "$p/lean:experiments_test"`                                                 |
| Compare module evaluation order across 500 graphs            | `bbr test "$p/es_order:es_order_sweep_test"`                                          |
| Fuzz the example renderer                                    | `bb run "$p/fuzz:render_fuzz" -- 600 1`                                               |
| Check translation fixtures and effect cases                  | `bbr test "$p/translation:fixtures_test" "$p/effects:effects_cases_test"`             |
| Run a larger translation fuzz sample                         | `bb run "$p/translation:translation_fuzz" -- --count 150 --seed 1 --require-accepted` |
| Run the extended CoreJS comparison                           | `bbr test "$p/model:corejs_differential_sweep_test"`                                  |
| Run the debundle case corpus                                 | [Corpus commands](debundle_cases/README.md#run-it)                                    |
| Lower public library bundles and check their splits          | [Bundle commands](experiments/README.md#6-lower-the-ast-into-lean)                    |

To refresh the checked-in example instance after changing the inputs:

```sh
dir=devinfra/js/debundle/x/split_proof
bbr build "//$dir/lean/generated:generate_instance" --remote_build_event_upload=all --remote_download_outputs=toplevel
cp "bb-out/bazel-out/k8-fastbuild/bin/$dir/lean/generated/EsmSplit/Instance.lean" "$dir/lean/EsmSplit/Instance.lean"
bbr test "//$dir/lean:instance_freshness_test"
```

Acorn supplies an unsplit module AST and eslint-scope annotates its binding occurrences. Lean enumerates source ordinals from the module body. It gives each declarator of a top-level `var`, `let` or `const` its own ordinal, including under a named export, and keeps each destructuring pattern together. Lean assembles an ESTree `Program` for each output file, including the imports and exports that connect the modules. Astring prints these trees as JavaScript. Functions, classes and methods retain their original source spelling because JavaScript exposes it through `Function.prototype.toString`. Unversioned input dumps are upgraded by reparsing the included source and checking its annotations.

For a closed module containing only identifier declarations, Lean also proves a `Declaration extraction` model result. Supported initializers include primitive literals, binding reads, unary minus on a number literal, and logical negation of a supported initializer (for example, `const offset = -42, visible = !!offset;`). The model allocates the entire module’s bindings before running initializers in order, and the proof includes throws. This model result alone does not authorize publication. [Declaration certificate](lean/EsmSplit/CoreJS/Declaration/Certificate.lean)

## Request a CoreJS guarantee

Pass the deployment manifest and a fresh output directory to the publishing splitter:

```sh
bb run //devinfra/js/debundle/x/split_proof/lean:lowersplit -- deployment.json output
```

Publication requires one strict guarantee: the requested placement and entry's public live cells are preserved, and all supported observable CoreJS endings agree, including throws and their effects. This is a claim about the modeled CoreJS programs under the supported JavaScript boundary, not a proof of general JavaScript semantics. The [target and trusted parts](PLAN.md#target) describe that boundary.

**Current acceptance:** no public constructor rule yet proves both the source/output bridge and the strict JavaScript observations. The splitter reports `reason_code: "no_constructed_rule"` when the required constructor evidence is unavailable; it does not publish a fresh output tree. `--check-output` likewise refuses without changing the existing tree. A model diagnostic or a proof of normal completion alone is not a publication certificate. Earlier replay and structural acceptances are not a current coverage promise.

The publishing command no longer accepts research switches to select replay or structural proof, request normal completion, or supply host/deployment premise overrides. There is no unchecked publishing mode.

### Developer diagnostics

Separate developer tools expose model experiments and example generation. The preview is test-only, is not certified output, and must not be used as a published split:

```sh
p=//devinfra/js/debundle/x/split_proof/lean
bb run "$p:split_preview" -- deployment.json scratch-output --force
bb run "$p:split_instance_generator" -- deployment.json Generated.lean
```

`split_preview` replaces the former `lowersplit --diagnostic` entry point; `split_instance_generator` replaces `lowersplit --generate-instance`. They serve example generation, tests and research, not the publishing contract. The [CoreJS differential inventory](model/README.md) describes model comparisons, and the [plan](PLAN.md#target) records the work needed to accept public splits.

## Historical replay and structural examples

The remainder of this section records the former replay and structural publishing paths, their commands, premises and example results. Those paths and flags have been retired from the publishing splitter. The examples describe historical model behavior and are not instructions for the current CLI or evidence that these inputs now certify.

An ordinary call to ambient `Symbol.for` can be retained in deferred code without a host assumption. If initialization reaches the callee, the model reports an unknown result before evaluating the arguments.

For browser module scripts, the `browser-page` contract also permits `.js` output paths on emission and disk checking.

For a module loaded as a browser page entry, you can pass `--host-contract browser.json` with `{"profile":"browser-page"}` on emission and on `--check-output`. Under this profile, ambient `window`, `globalThis`, and `self` may return the same opaque page object, and their `typeof` may return `"object"`. The browser fallback `typeof global` may return `"undefined"`; a page with a Node-style shim can violate that premise. Each selected observation must preserve host state throughout initialization. A direct read of ambient `global` remains opaque and stops the proof if reached. Lexical and imported names use their actual bindings.

The [browser globals catalog](lean/EsmSplit/CoreJS/BrowserGlobals.lean) covers ECMAScript and browser APIs, including DOM bindings, timers, networking, storage, and static method references such as `Object.defineProperty` and `Date.now`. A reached lookup can assume a specific `typeof` or stable reference. For example, a reference to `FileReader` requires it to exist on that page. Reading `window.Error` and reading the bare `Error` binding can return the same opaque reference under separate premises. The checker records observations from both initializers and the generated Lean theorem checks them in every world satisfying the listed premises. A deferred or untaken lookup adds no premise. A reference to a method supplies no result for a call. Volatile page values, including scroll coordinates, and operations such as reading Error stacks or writing to the DOM stay opaque.

CoreJS allocates a fresh object for each regular-expression literal. It preserves the pattern, flags, and `lastIndex` descriptor. The shared prototype implements `source`, the individual flag getters, and `flags`, including reads of overridden flag properties. Calls to the intrinsic matching methods currently produce an unknown result.

The browser-page profile also allows deferred construction of selected ambient constructors (`Date`, `Error`, `RegExp`, `Set`, and `URL`). The linked model retains the constructor name and arguments. If initialization reaches that lookup, it reports an unknown result before evaluating the arguments, including inside a `try` block. Reading `Date.now` can certify under a stable-reference premise. Calling it during initialization requires a model of the clock. The caller is responsible for choosing a profile that holds in the deployment.

Linked replay models `super.member` and `super[key]` reads, direct calls, and
optional calls. It starts at the current prototype of the method's home object
and passes the active receiver to getters and calls. An arrow retains both the
home object and receiver of its enclosing scope. Async bodies can retain these
references, but replay stops if it reaches an async invocation. Super-property
writes and deletion remain outside the executable model.

An async definition can fail during lowering if its body needs context the
checker cannot retain. The `linked_input` verdict identifies the original unit,
callable number within the unit, and failed check. A `probe_unknown` verdict
that reports an async invocation means the linked initializer reached the call.

Class static blocks run in their own scope with the constructor as the
receiver. The checker evaluates class member keys before it initializes
fixed-name static fields and runs blocks in their source order.

The command prints one JSON verdict. A successful split reports `status: "certified"` after Lean certifies the output module ASTs and the printer writes them to the requested directory. The default `normal-completion` request means that, for each modeled world and successful final state, the bundle reaches that state exactly when the split does. It does not say that either program completes normally; in the closed-declaration case, both may throw. The linked-program proof additionally establishes that both programs initialize normally. The checker reports the strongest available guarantee. A structural split that links to the same initial state and the same ordered units reports `all-outcomes`, including when both versions throw. Pass `--require all-outcomes` to insist on that stronger guarantee. A rejected request exits nonzero with `status: "rejected"`, a null guarantee and a reason.

To move an initializer such as `Math.PI / 12`, pass
`{"profile":"browser-page","stableIntrinsicValues":["Math.PI@1"]}` with
`--host-contract` and `--structural` on both emission and `--check-output`.
The caller asserts that throughout initialization the ambient binding returns
the same Math object, and that reading its own `PI` data property returns the
Number with binary64 bits `0x400921fb54442d18`. Both reads must have no
observable effects. The verdict lists the premise when the move uses it.
A page that replaces `Math` with `{PI:24}` before the declaration can get
`2` in source order but `Math.PI / 12` from the original intrinsic after the
move. Such a page does not satisfy the contract. Neither a lexical `Math`
binding nor an object operand requiring numeric coercion qualifies for this
rule. The plain browser-page profile makes no such assertion.

For the pinned Node semver bundle, `{"profile":"ecmascript","stableIntrinsicValues":["Object.defineProperty@1"]}`
lets the structural checker move `var saved = Object.defineProperty;`. Throughout initialization,
reading the ambient `Object` binding must return the same receiver, and reading its own data
property `defineProperty` must return the same method reference. Both reads must preserve host
state. Deployments that replace the method or install an observable getter can violate the premise.
The contract grants no behavior for invoking the method and does not require browser globals.

Use one deployment manifest for the original loader graph. Each changed chunk
embeds one record from `debundle spec resolve-placements`; a fixed chunk has
only its original route and source. Resolve physical source paths relative to
the manifest. The following shell assembles a manifest for two changed chunks
from a batch export:

```sh
jq '{entry:"main.js", chunks:[.placements[] | {
  path:(.chunk_id + ".js"), source:("bundle/" + .chunk_id + ".js"), placement:.
}]}' resolved.json > deployment.json
printf '%s\n' '{"profile":"browser-page"}' > browser.json
lowersplit deployment.json out --structural --host-contract browser.json --deployment-origin https://example.test
lowersplit deployment.json out --structural --host-contract browser.json --deployment-origin https://example.test --check-output
```

Use the original module routes in `path`, including hashed filenames when the
loader uses them. Multi-chunk structural checks read the routes from one original asset tree. The checker verifies each source digest and ordinal, keeps
unclaimed units at their original routes, and checks one staged tree against
the linked execution from `entry`. Each selected owner gets a distinct `.mjs`
route. A fixture can supply `spec` in place of `placement` to use a handcrafted
positional or binding-name plan.

The untrusted planner derives module requests from the bindings each owner needs. Capturing a
binding can require an import without reading its value at initialization. The authored priority
orders requests. ES module evaluation visits dependencies before finishing the importing module
and skips an already entered module in a cycle. The structural certificate checks the resulting
order and actual live cells. A refusal applies to that proposal; it leaves other schedules unknown.
For a required execution order, supply a regular spec with `sequential: true`.

A declaration such as `const width = 2 * spacing` can move when the checked
prefix in each layout establishes the Number in the linked `spacing` cell.
The result can supply a later arithmetic initializer. The proof retains the
operator tree and uses CoreJS Number operations; the emitted code still reads
the binding. Unknown writes, object coercions and uninitialized cells do not
provide Number facts.

The same prefix check can certify creation of a fresh record such as
`{[fontName]: 100, defaultTab: libraryTab}` when the two cells hold
the same respective strings in both layouts. Lean checks key and value
evaluation and every data property definition on the new object. It keeps
the original allocation and field order; an object key requiring coercion
does not satisfy the string-read proof.

For a move such as `const chosen = table.value`, the structural checker can
prove the read from a freshly allocated record with distinct own numeric
data properties. It checks the receiver's actual linked cell and descriptor
in each schedule, including through a local alias. A write, accessor or
unmodeled intervening operation drops the evidence. Only the scheduling
proof substitutes the value; the emitted JS still reads `table.value`.

The experimental `--structural` checker selects a proof from the linked schedules. If the units keep their original order, it checks the exact unit sequence, source syntax, resolved capture cells, initial state and entry exports, then reports `scope: "linked-structural-order"` and `guarantee: "all-outcomes"` when the decoded schedules match exactly. For an order change, it brings units forward one at a time. A hoisted function is already in its declaration cell before either schedule runs. The initial states must agree on its object address, code, and captured cells; entry exports must resolve to the same live cells. Only the inert declaration step moves past earlier units, which may already use the function. Moving an allocating initializer still requires checks on the prefix and crossed units of the intermediate schedule. It reports `scope: "linked-structural-pivot"` for one move and `scope: "linked-structural-moves"` for multiple moves.

A regex literal can move as a fresh allocation, including as the value of a named data property. Each evaluation creates a distinct object with its own `lastIndex` and the realm's `%RegExp.prototype%`. The intrinsic constructor's `prototype` is a non-writable, non-configurable data property; literal evaluation does not consult the global `RegExp` binding or invoke getters on the prototype. This rule needs no browser-page premise. Calling `new RegExp(...)`, reading `/a/.flags`, and running a matcher during initialization remain outside the allocation rule.

Both proofs establish equality of normally terminating CoreJS states. Retained opaque statements use the same world on both sides, with access through their checked live cells and object handles. For a retained generator declaration, the structural checker uses the linked generator model at instantiation, including its distinct prototype and captured cells. This does not model generator execution or resumption. The verdict records the assumptions for interpreting the JavaScript artifacts.

[Checking an assembled deployment](docs/joint_static.md) describes the scoped joint checker and the additional deployment premises needed for a main/editor split.

A static browser deployment with retained `import.meta.url` and literal relative `import()` can supply `--deployment-origin ORIGIN`. The source entry keeps its effective URL, the split overlays its planned file paths, and the other assets keep their bytes and URLs. The checker parses the actual lazy-module dependency closure, accounts for its imports from linked modules, and checks the live cells those imports resolve to. The host uses native module caching and promise scheduling. Source positions and fetch timing are outside the modeled observations. Pass the same options to `--check-output` to repeat the proof after reparsing the emitted JavaScript.

With the `browser-page` host profile, `--deployment-preset native-browser-record@1` adds an **initialization** premise to `--deployment-origin`. Each original referrer's metadata starts with the native browser shape: a fresh null-prototype object with writable, enumerable, configurable own `url` and `resolve` data properties. The per-record resolver uses its original referrer URL and the same static resolution context. Creating these objects has no observable side effect. A loader that preinstalls an `env` getter does not satisfy this premise; the splitter does not detect a lying host declaration.

For a page whose import map remains empty, `--deployment-import-map empty@1` recognizes literal, unmapped package requests in retained units, for example `import("@scope/diagram")`. This is a deployment premise, including when a deferred callback runs. It requires the browser profile and checked deployment origin. The location checker records a rejected import: native resolution fails before there is a target URL, fetch, or module namespace. The original import stays in the opaque code. Each call still returns a promise, including the browser's rejection, reaction jobs, and unhandled-rejection reporting. Use the same flag for disk verification. A page that installs a map later, or uses a custom resolver, does not meet this premise. Computed requests, URL schemes and absolute paths do not use this rule.

[Lean's initializer](lean/EsmSplit/CoreJS/NativeRecord.lean) allocates that receiver and its nonconstructible resolver in a private model dependency before original module bodies run. Each referrer captures its immutable binding. The resolver runs in the common opaque world with its actual receiver and arguments. The record's properties can subsequently change; getter calls and objects reachable from them remain subject to the ordinary structural frame checks. All metadata sites, including deferred ones, must stay at their original referrer. The model dependency is never emitted. Pass the same preset to `--check-output`; without it the original deployment checks still apply.

The verdict's `scope` identifies which theorem the run established. Each theorem is about the CoreJS programs that Lean lowers from the original annotated ESTree and the [generated module ASTs](lean/EsmSplit/ESTreeEmit.lean):

- [Closed declarations](lean/EsmSplit/CoreJS/Declaration/Artifacts.lean): `prepare?` checks the plan, footprints and order. Its `certify` function is a proven checker that runs as compiled code. It decodes the output modules, checks the entry's public surface, and proves the reported level for those modules. Kept order gives equal results and states for every world and fuel, including throws. Checked moves give the same successful states in both directions.
- [Structural order and moves](lean/EsmSplit/CoreJS/StructuralCertificate.lean): `certifyStructuralASTs` is a proven checker that runs as compiled code. It checks both linked schedules. Its result proves that each entry exports the same live cells and that either graph reaches a successful final state exactly when the other does, under the opaque-world and deployment interpretation above.
- [Compiled slices](lean/EsmSplit/LowerSplit/SliceCertificate.lean): the checker uses the empty lowering profile and proves the reported guarantee for the exact decoded CoreJS pair. It checks the requested placement and that each entry export resolves to the same live cell. Kept order gives equal results and states for every CoreJS world and fuel. Checked moves give the same successful states in both directions. Deployments that require a host profile or have source ordinals omitted from the decoded program can use the other linked checks.
- [Compiled linked replay](lean/EsmSplit/CoreJS/ProbeReplay.lean): the checker transports supported successful probe runs to every world satisfying the reported host premises. It checks the linked programs and their entry exports with compiled proof-carrying functions. Requests outside the available compiled rules are rejected with `reason_code: "no_compiled_rule"` when no rule applies.

[Publication](lean/EsmSplit/SplitFiles.lean) prints to staging, reparses the files, and checks their module set, syntax, and callable spelling. It requires copied providers to match the source text. The certificate consumes the annotated ASTs from those disk reads. `--check-output` repeats the same checks on an existing directory, including for regrouped declarations with the same ordered units. The theorems relate CoreJS programs. Their application to the files relies on the parser and scope annotator, the lowering and CoreJS model, and Lean's compiler and file IO.

`CoreJS.CheckedSplit.checkedSplit` constructs an order-preserving split of a single import-free CoreJS entry. The fragment covers simple `const` declarations, including direct zero-argument calls, and ordinary zero-argument function declarations that return a literal or binding. Each success carries the requested placement, the same public live cells, and the requested guarantee in every CoreJS world. A companion check compares an independently decoded artifact with the constructed program. A Bazel fixture exercises the API from prebuilt Lean modules. The `lowersplit` command uses the production checkers above. [Constructor and proof](lean/EsmSplit/CoreJS/CheckedSplit.lean)

The test-only `checked_artifact_test` exercises the constructor from a JavaScript file and authored spec. It prints the constructor's plan with the existing emitter, reparses the staged files, and requires their linked lowering to equal the constructed graph before publication. It also checks the same graph on disk, rejects tampered files and keeps prior output intact on refusal. The tracked Bazel test uses the standard Lean launcher for the test entry. The native version of the fixture uses the same JavaScript frontend and staging code. [Artifact verification](lean/EsmSplit/LowerSplit/CheckedArtifact.lean)

For a module that calls functions or imports named bindings from relative JavaScript files, use
the manifest command with the same original source path. The splitter parses
reachable `.mjs` and `.js` providers as ES modules and copies them alongside the split.
The compiled checker lowers the original and generated module ASTs to CoreJS and checks statement placement, live binding addresses and the entry's exports. Its accepted replay rules prove normal completion in the same final state for every modeled world satisfying the reported host premises. When no compiled rule covers the request, the command rejects it without publishing output; `--check-output` rejects it without changing the existing tree. Neither command starts Lean or elaborates a proof at runtime.

The linked checker can retain an ordinary function whose body fails lowering. It checks the callable's source spelling, original unit and lexical position, and captured bindings in both trees. The interpreter allocates the closure and its properties as usual. Calling or constructing it then yields an unknown result with the body’s lowering error. Retention requires that the callable can be represented without a module URL, method home object, or direct `eval`.

A named export list such as `export { local as publicName }` can publish an imported binding.
Imports can follow a chain of these aliases to the declaration's live cell. A chain that
cycles without reaching a declaration fails to link. Lean checks that each entry export
resolves to the same live cell in both graphs. Source-bearing re-exports such as
`export { name } from "./provider.mjs"` are outside this fragment.
When a generated import needs a binding already published under an alias,
it imports that public name under the original local name. This adds no entry export.

Linked code can define async functions, arrows and methods. Each callable retains its source body and captured bindings, has the declared name and arity, and inherits from the realm’s `AsyncFunction.prototype`. A call reached during initialization returns an unknown verdict because CoreJS does not execute promises or schedule continuations. Async callables that need a home object, private names, direct eval or module context are rejected during lowering.

Synchronous and async generator declarations, expressions and methods retain their source and captures. Async generators have distinct shared function and iterator prototypes. Each function has a fresh prototype inheriting from the realm's corresponding generator prototype. Initialization can inspect those objects and their properties. Calling a generator during initialization yields an unknown verdict because parameter initialization and generator suspension are outside this model.

Ordinary top-level function declarations initialize when the modules link. A call can reach a declared function before its statement executes, while its closed-over lexical bindings still observe their own initialization order. When a supported parameter list precedes a body CoreJS cannot execute, the checker can retain the callable with its captures and declared arity. This includes functions with defaults, methods, accessors and base constructors. Array, rest and nested destructuring parameters can also be retained with their original captures and arity. Invoking the retained body during initialization reports its original lowering failure. Ordinary declarations directly in a function body also hoist, provided their names are distinct and do not overlap parameters or `var` bindings. Block-level function declarations and generators remain outside this fragment. [Linked lowering](lean/EsmSplit/CoreJS/FromESTree.lean)

Named parameters can have defaults such as `(x = 1, y = x) => y`. Missing and `undefined` arguments use the initializer. A `null` argument binds directly. Parameters initialize left to right and a read of the current or a later parameter before initialization throws, including inside `typeof`. Body lexical declarations are in a scope inside the parameter environment. The linked fragment refuses a body `var` whose name repeats a parameter or occurs free in an initializer. The function's `length` counts only parameters before the first default. CoreJS executes defaults on identifier parameters and on flat object patterns. It can retain other parameter patterns for deferred calls. Strict ordinary functions and methods bind each invocation's actual arguments before evaluating defaults. The object has independent indexed properties, `length`, a throwing `callee` accessor, and the intrinsic array-values iterator. An arrow captures the enclosing binding.

Module-level `var`, `let`, and `const` declarations can use object binding patterns, including nested object patterns, computed keys, and defaults. The module linker creates a cell for each bound name. The declaration evaluates its initializer once, then reads properties and initializes bindings in source order. A getter keeps its receiver, and a default runs only for `undefined`. It can read an earlier binding, while a read of a later lexical binding throws in the temporal dead zone. An empty pattern still checks that the source is neither `null` nor `undefined`. Object rest remains unsupported.

Local declarations use the same object binding rules. Linked ordinary functions and arrows also accept nonempty flat object patterns with identifier or string keys in parameters. For example, `function read(first, {x, label: name = first} = {})` has length one. If its second argument is missing or `undefined`, the function evaluates `{}` before reading `x` and then `label`. Passing `null` throws during parameter initialization. These rules also apply before the first `super()` in a derived constructor.

In ordinary functions, arrows, methods and base constructors, a parameter may end in an object-rest identifier, as in `({ tab, ...options }) => options`. Static selected properties are read in order before the rest object copies enumerable own properties, including symbols, except the selected keys. A rest-only pattern still rejects `null` and `undefined`. Defaults, computed keys and nested bindings with object rest remain deferred; derived constructors still refuse them.

Array binding patterns and synchronous `for…of` loops take the source's iterator and cache its `next` method. Array patterns support elisions, defaults, nested bindings and a trailing rest binding. An elision advances the iterator without reading the iterator result's `value` property. A lexical loop head creates fresh cells for each iteration. An assignment head can write an identifier or a property. The iterator's `return` method runs if binding or the body exits abruptly, and when a finite array pattern leaves the iterator open. The native array path checks the iterator method and the array iterator prototype. String iteration, host iterator methods, and `for await…of` remain unsupported.

In ordinary functions, arrows, methods and base constructors, a terminal rest parameter may use a flat array pattern of identifiers and elisions, as in `(...[first, , third]) => first + third`, after zero or more identifier parameters. The remaining arguments are packed into a fresh array and the pattern obtains its iterator, so an overridden array iterator and its closing behavior remain observable. Defaults and nested patterns in this rest binding remain deferred; derived constructors still refuse it.

In linked programs, conditional expressions and `if` statements inside functions evaluate the test once and run the selected branch with the state left by the test. Both branches must lower to CoreJS, including code that initialization never executes. Branch blocks have their own lexical scope, and `var` declarations in either branch belong to the enclosing function. A false test with no `else` continues to the following statement.

Linked functions can use `switch` with a trailing `default` and case bodies that exit before the next clause. The discriminant is evaluated once, and case tests run in source order and match by strict equality. Empty labels share the following body. A break exits its nearest loop or switch. A continue inside a switch reaches the enclosing loop. Braced case bodies can declare their own lexical bindings.

Linked sequence expressions evaluate their operands from left to right, stop at the first abrupt completion, and return the final operand's value. A sequence used as a call's callee supplies `undefined` as `this`, including when its last operand reads a member.

Linked member expressions support dot access and computed keys with modeled primitive
property names. A member call evaluates its receiver and key once, reads the callee before
evaluating arguments, and passes the receiver as `this`. Ordinary functions have the call's
receiver and arrows capture the surrounding `this`. Access to private members and to `super.property` is unsupported.

In the `browser-page` profile, a class can define a method or accessor at `[Symbol.iterator]`.
When a read executes, the certificate records the premise that the ambient `Symbol.iterator`
returns the well-known iterator symbol without observable effects. Its descriptor is distinct
from one named `"Symbol.iterator"`. For example, `this.path[Symbol.iterator]()` looks up the
method on the path’s prototype chain and calls it with the path as its receiver.

A browser-profile call to the ambient `Symbol()` or `Symbol("description")` creates a fresh
symbol under an explicit premise that the binding holds the intrinsic Symbol function.
A class can use a lexical identifier as a computed instance field key. Class definition
captures the key once, and each construction creates an own data property for that key.

A derived class initializes its public instance fields in declaration order on the object returned
by its superclass. Its default constructor forwards the original arguments. An explicit constructor
initializes the fields after `super(...)` succeeds and before its next expression. Initializers
resolve lexical names in the class definition scope. Derived static fields remain unsupported.

An explicit derived constructor can use named parameter defaults. Missing or `undefined` arguments
trigger defaults from left to right, before the constructor body or its direct `super(...)` call.
Later parameters stay in the temporal dead zone, and the first default determines the constructor's
`length`. Defaults follow the same receiver restrictions as the prefix. A body binding that would
shadow an outer name used in a default is rejected.
Before `super()`, `var`, `let`, and `const` declarations can bind an object pattern. A trailing
`...rest` copies enumerable own properties, including symbols, after the selected fields have
been read. Each selected key must lower to a constant string for this rest binding.
A direct read of `this` in the prefix or arguments is rejected. Arrows can capture the receiver
cell and read it after the parent initializes it. The parent receives the evaluated arguments
in source order. The rest of the constructor sees the same local bindings and the receiver returned by
the parent, including a replacement object. If argument evaluation changes the active constructor’s
prototype, CoreJS returns unknown because Node and ECMAScript can select different parents.

Consecutive optional member reads such as `receiver?.field?.[key]` evaluate each reached receiver once. A null or undefined receiver yields `undefined` without computing a key or invoking a getter. A reached read keeps the original base as the getter's `this`, even if key evaluation changes the value of the receiver expression.

A single named method call `receiver?.method(args)` skips lookup and arguments when the receiver is null or undefined. The form `receiver.method?.(args)` performs ordinary property lookup first and skips the arguments if the returned method is null or undefined. Both forms evaluate the receiver once and preserve it as `this`. Bare optional calls, computed optional method calls, combined optional receiver and callee, chains with non-optional continuations, and chain expressions used directly as call targets still receive a refusal.

Linked `key in base` evaluates the key expression and then the base expression.
A primitive base throws before the key is converted. On a modeled object, CoreJS
converts the primitive key and looks for a property on the object or its prototype
chain without calling a getter. Reached object-to-primitive conversion stops with
an unknown result. Symbol keys, proxies, and unmodeled host objects are outside
the fragment.

Simple assignments to dot and computed properties, such as `record.name = value` and `record[key()] = value`, use CoreJS strict property writes. CoreJS evaluates the receiver, computed key expression, and right-hand side in that order, writes through the original receiver, and returns the assigned value. Object-key coercion, host writes, and array writes can produce an unknown result during initialization.

Plain `=` also writes local and module bindings. It evaluates the right-hand side once before writing and returns the assigned value. An anonymous function on the right-hand side takes the target identifier's name. An assignment through an existing import evaluates its right-hand side and then throws a `TypeError`. An assignment reached before its lexical binding initializes or after a `const` binding initializes also throws in CoreJS. Placement still requires writes to source module bindings to stay with their declarations. Destructuring and global assignments remain outside the linked lowering.

Arithmetic and bitwise compound assignments read the target before the right-hand side runs, apply the corresponding binary operator, and return the value they write. This includes `+=`, `-=`, `*=`, `/=`, `%=`, `&=`, `|=`, `^=`, `<<=`, `>>=`, and `>>>=`. The logical assignments `&&=`, `||=`, and `??=` read the same kinds of targets and short-circuit both the right-hand side and the write. Their anonymous functions receive the same identifier name as with plain assignment. Prefix and postfix `++` and `--` convert a modeled primitive to a number, write the updated number, and return the new or old number respectively. Compound assignments and updates target local or module bindings or properties.

Linked `delete` accepts dot and computed member expressions. It evaluates the receiver and key, then removes an own configurable property. A missing own property succeeds, while a nonconfigurable property throws in strict code. Reached deletion on unmodeled values or a computed key that requires object coercion stops the model.

Linked object literals may contain static and computed data properties, methods and accessors, and spread elements. A numeric literal key uses the JavaScript string form of its value, so `5e-1` names the property `"0.5"` and the quoted key `"5e-1"` keeps its spelling. Property values use the same linked expression lowering as other initializers. CoreJS creates a fresh ordinary object and initializes its own properties in source order. If one property's value throws, evaluation stops before the following property. Repeated definitions update or extend the existing property without changing its first insertion position. A colon-form `__proto__` sets the prototype. A computed property evaluates its key before the value and defines an own property even when the key is `"__proto__"`; methods and accessors take their inferred names from the converted key. The Node comparison cases exercise `ToPropertyKey` for strings, numbers, booleans, null, and the well-known iterator symbol. The `browser-page` production profile supplies the well-known symbol intrinsic; the default `empty` and `ecmascript` profiles make no ambient `Symbol` assumption. A directly anonymous function or arrow value still needs name inference from the computed key. An explicitly named function keeps its name. Object-key coercion that invokes user code remains outside the executable model.

Object spread evaluates its operand once and copies its enumerable own properties into the new object. Keys follow JavaScript enumeration order. Getters run with the source as their receiver and can change which later keys are copied. A nullish value, number, or boolean contributes no keys. A string contributes its indexed UTF-16 code units within the Basic Multilingual Plane. Copying a host value, an engine error, or a string containing a supplementary character yields an unknown result.

A logical expression in a static data property, such as `{mode: config.mode || "default"}`, evaluates its left operand first. Only the selected operand supplies the property value, and an abrupt completion stops construction of the record.

A linked `void` expression evaluates its operand and returns `undefined` on normal completion. Side effects and thrown exceptions from the operand are observable.

Dense array literals, calls and constructors evaluate each ordinary or spread operand left to right. A spread operand consumes its synchronous iterator before evaluation proceeds to the next operand. The iterator model covers linked iterator methods and the native array iterator with its modeled next method. Sparse literals with primitive literals, including negative numeric literals, or unshadowed `undefined`, `NaN` and `Infinity` preserve missing own indices and array length. Sparse literals containing other binding reads, spreads or other expressions remain refusals.

Untagged template literals use their cooked text and evaluate substitutions from left to right. CoreJS converts each primitive substitution to a string before evaluating the next expression. A linked initialization that needs object-to-primitive conversion reaches an unknown result and cannot certify.

A tagged call evaluates its callee and preserves a member tag's receiver. Each lexical site has its own frozen cooked array and frozen `raw` array, shared across evaluations of that site in one linked realm. Invalid tagged escapes yield `undefined` cooked entries. Substitutions pass their actual values to the tag in evaluation order. Under browser assumptions for the intrinsic `Array` binding and its `from` method, `Array.from(strings)` copies template elements through the intrinsic iterator into a fresh mutable array. Replaced iterator methods and isolated UTF-16 surrogates are outside this replay path.

Linked binary `+`, `-`, `*`, `/`, and `%` evaluate both operands in source order and then apply CoreJS primitive coercion. Addition concatenates when either primitive operand is a string. Numeric operations use binary64, including signed zero, infinities, NaN, and JavaScript’s signed remainder. Linked unary `+` and `-` convert a primitive operand to a number. The bitwise operators `~`, `&`, `|`, `^`, `<<`, `>>`, and `>>>` reduce numeric operands to 32 bits and mask shift counts to five bits. Unsigned right shift returns the unsigned result. Object-to-primitive conversion and host values reach an unknown result when evaluated. BigInt, Symbol, and exponentiation remain outside the linked fragment.

Linked `<`, `<=`, `>`, and `>=` evaluate operands in source order. CoreJS compares two strings by UTF-16 code unit and converts other primitive pairs to numbers. A numeric comparison with NaN is false. String literals, template text and property keys can contain supplementary Unicode characters. String length and indexed reads use UTF-16 code units. For example, `"A😀B".length` is 4 and `"A😀B"[3]` is `"B"`. A read that would return an isolated surrogate gives an unknown result. The lowerer rejects literal and template text containing an isolated surrogate. Reached object-to-primitive conversion remains unknown.

Linked expressions support `typeof`, `===` and `!==` on modeled operands. Strict comparisons evaluate both operands in source order and compare without coercion. Loose equality and inequality compare two strings by contents, compare ordinary objects by identity, and perform JavaScript numeric conversions for mixed string, number and boolean operands. A nullish comparison can finish without coercing an object. Reached comparisons requiring an object to produce a primitive or inspection of an opaque host value stop the checker. Looking up a host global still requires a model of that binding, including inside `typeof`.

The linked fragment handles `try` inside function bodies with `catch`, `finally`, or both. A catch can bind one identifier or omit the binding. Inside functions, grouped `var`, `let` and `const` declarations with identifier binders initialize in source order. Each block has its own lexical scope, and an abrupt completion from `finally` takes precedence over a pending return or throw.
The state includes the heap and captured bindings of exported closures. The checker
runs both programs in a concrete `World.probe` to select an interpreter fuel witness.
When both finish normally, it compares the complete final state: heap objects and
closure code/environments, binding cells, allocation region, logged output, handed
object handles, and the probe's unit world state. Floating-point values are compared
by their IEEE bit patterns. If these probe states differ, `Ends.unique` proves that
the programs cannot finish with one shared state in every world, so the checker
refuses before generating the all-world proof. Matching probe states are only a
necessary check; Lean still proves the guarantee for every modeled world.

When both linked graphs run their units in the same order, Lean can reuse the
source’s normal execution. It checks the state after hoisting and compares each
unit’s original ordinal, full statement, receiver and free-name binding cells.
The entry’s public names must resolve to the same cells in both graphs. A mismatch reports the counts of extra, missing, and retargeted names, with up to three examples of each. An address `@region.index` identifies a live cell: top-level unit `i` allocates its bindings in region `i + 1`. For a retargeted name, the two addresses show which declarations to inspect in the original and candidate graphs.

The checker starts at 16 fuel and retries at 32 and 64 on fuel exhaustion. For a
top-level record initializer it can make one more attempt with 64 plus the largest
property count, capped at 2048. Exhausting the budget, unsupported syntax or an
all-world proof that fails to reduce yields a rejection. A successful comparison
reports `"scope":"linked-programs"`.
[AST certificate](lean/EsmSplit/CoreJS/LinkedCertificate/Proof.lean) · [Linked behavior theorem](lean/EsmSplit/CoreJS/LinkedBehavior.lean)

### Read a linked refusal

Linked refusals from AST lowering and the concrete probe include a `reason_code`. A `linked_input` refusal means the input failed linked lowering or linkage. `probe_fuel` means the search exhausted its budget. `probe_mismatch` means both probe executions finished with different states. Its optional `observation` reports a differing exported value or object alias when one is visible in exported cells or their own data properties. Paths list the export name and, for an own property, the property name.

### Measure certification

Set `SPLIT_PROOF_TIMINGS=1` to write phase names and elapsed milliseconds as JSON lines on stderr. Stdout carries the certification verdict. The phases include staging, output comparison, lowering and the selected compiled checks.

For public comparisons, use the wide-table reversal and the 48-reader linked module graph:

```sh
p=//devinfra/js/debundle/x/split_proof
bbr test "$p/translation/linked:wide_table_reversal_test" "$p/translation/linked:module_graph_scale_test" \
  --test_env=SPLIT_PROOF_TIMINGS=1 --test_output=all --nocache_test_results
```

Compare the same fixture, proof mode and runner across revisions. Record peak memory separately from the phase times.

### Inspect an uncertified preview

The test-only preview tool runs structural checks and prints model diagnostics.

```sh
bb run //devinfra/js/debundle/x/split_proof/lean:split_preview -- deployment.json scratch-output
```

Use `--plan <file>` to inspect module order, statement placement and conflicting pairs, or `--force` to emit a preview of a rejected plan. Successful diagnostic runs also produce uncertified previews. Experiments can pass `--corejs <program.json>` here to supply an external lowering by statement ID. [Move rules and proof inventory](docs/footprint_soundness.md)

## The example

This is an abstract plan-checking and renderer example, not a current publishing certificate.

Input, [`example/input/bundle.mjs`](example/input/bundle.mjs). It has 12 top-level statements, numbered 0–11:

```js
const PI = 3.14159; // 0
function square(x) {
  return x * x;
} // 1
const area = (r) => PI * square(r); // 2
function isEven(n) {
  return n === 0 || isOdd(n - 1);
} // 3
function isOdd(n) {
  return n !== 0 && isEven(n - 1);
} // 4
const registry = []; // 5
function register(name, fn) {
  registry.push(name);
  return fn;
} // 6
register("area", area); // 7
const label = (s) => "<" + s + ">"; // 8
register("label", label); // 9
console.log(label("circle"), area(2), isEven(10)); // 10
console.log(registry.join(",")); // 11
```

Spec, [`example/spec.json`](example/spec.json). A selector is a declared name, or `#n` for a statement that declares nothing:

```json
{
  "entry": "main.mjs",
  "modules": {
    "utils.mjs": ["PI", "square", "area", "isEven", "isOdd", "label"],
    "registry.mjs": ["registry", "register"],
    "plugins.mjs": ["#7", "#9"],
    "main.mjs": ["#10", "#11"]
  }
}
```

Two optional keys set the order the modules run in. `"order": ["a.mjs", …]` is an import priority: the entry imports every module in that order. That does not fix the order of evaluation, because a module runs after the modules it imports. `"sequential": true` does: the modules run in exactly `order` (or, without `order`, in the order of their first statements), the entry last. The planner then makes a module that an earlier one mentions import that earlier module first, and every module between ([`Split.lean`](lean/EsmSplit/Split.lean), `nest`). `Valid` checks the resulting plan like any other.

For certification, the authored spec has a closed set of keys: `entry`, `modules`, `order`, `sequential`, and `privateEntryBody`. Unknown keys, duplicate keys, and values of the wrong type are refused. Resolved placements use their own format.

The `render` target writes these files to the output directory you pass it. Statements are copied verbatim.

```js
// main.mjs
import { label, area, isEven } from "./utils.mjs";
import { registry } from "./registry.mjs";
import "./plugins.mjs";

console.log(label("circle"), area(2), isEven(10));
console.log(registry.join(","));

// plugins.mjs
import { area, label } from "./utils.mjs";
import { register } from "./registry.mjs";

register("area", area);
register("label", label);

// registry.mjs
const registry = [];
function register(name, fn) {
  registry.push(name);
  return fn;
}

export { register, registry };

// utils.mjs
const PI = 3.14159;
function square(x) {
  return x * x;
}
const area = (r) => PI * square(r);
function isEven(n) {
  return n === 0 || isOdd(n - 1);
}
function isOdd(n) {
  return n !== 0 && isEven(n - 1);
}
const label = (s) => "<" + s + ">";

export { area, label, isEven };
```

The bundle runs statements in order `0 1 2 … 11`. The split runs `0 1 2 3 4 8 5 6 7 9 10 11`.
`label` (8) now runs before `registry`, `register` and the first registration. The proof says why that is fine.
Both programs print `<circle> 12.56636 true` and `area,label`.

## What is proved

The abstract theorem below applies to a plan and its stated footprints. No verdict states it
directly; closed-declaration verdicts reuse its `Valid` checks. For the artifact guarantee of
the normal splitter, see [CoreJS certification](#request-a-corejs-guarantee).

[`lean/EsmSplit/Correct.lean`](lean/EsmSplit/Correct.lean):

```lean
def Correct (B : Bundle) (P : Plan) : Prop :=
  (P.execOrder.Nodup ∧ ∀ x, x ∈ P.execOrder ↔ x < B.length)      -- every statement runs exactly once
  ∧ Hygienic B P                                                   -- every name means what it meant
  ∧ ∀ (V W : Type) (M : Meaning V W) (s0 : St V W),                -- whatever the statements mean:
      B.exec M s0 P.execOrder = B.exec M s0 (List.range B.length)  -- same values, same world

theorem split_correct {B : Bundle} {S : Spec} {P : Plan} (h : Valid B S P) : Correct B P
```

For any statement semantics satisfying `Meaning`, an accepted plan executes
each statement once, preserves binding resolution, and leaves the same
top-level values and world state.

`Valid` is a conjunction of decidable checks ([`Plan.lean`](lean/EsmSplit/Plan.lean)):

| Check                       | Says                                                                                                                  |
| --------------------------- | --------------------------------------------------------------------------------------------------------------------- |
| `Faithful`                  | each statement sits in exactly the module the spec names; no selector is dangling                                     |
| `Linkable`                  | each import names a binding another module declares and exports; no name is bound twice; each glue export is declared |
| `ExportsUnique`             | no module exports a name twice: its glue keeps clear of the names its own `export` statements publish                 |
| `Hygienic`                  | each name a statement mentions resolves, through imports, to the declaration it had in the bundle                     |
| `AssignsHome`               | code that assigns a name sits in the module that declares it (ES imports are read-only)                               |
| `Permutes`                  | the split runs every statement exactly once                                                                           |
| `OrderOk`                   | in the order the split runs them, conflicting statements keep their bundle order                                      |
| `UniqueDefs`, `ReadsClosed` | side conditions: one flat scope; read sets are closed under calls                                                     |

A statement **binds** the names it declares and the names it may assign (`writes`: reassigned variables, objects whose contents it changes,
including what the functions it calls change; unknown code lists them all). These named mutable things are _cells_.
A statement may also read names it does not mention, through a function it calls that it reached through an object (`deps`); they order it like `eager` names do.
Two statements **conflict** ([`Bundle.lean`](lean/EsmSplit/Bundle.lean)) if both have effects,
or one reads a name the other binds (either direction), or both bind the same name.
A hoisted `function` declaration never conflicts: it exists before any code runs.
Everything else may be reshuffled freely. `assigns` is a different list: where the code that assigns a name physically sits, which decides where it must live (`AssignsHome`).

The order the split runs in is computed by `Plan.visit`: a model of ES module evaluation.
It goes depth first over `import`s in source order, runs each module once, and skips a module that is already being entered.
That last rule cuts import cycles.

[`Semantics.lean`](lean/EsmSplit/Semantics.lean) proves pairwise commutation of
nonconflicting statements (`step_comm`) and lifts it to the plan's execution
order (`exec_reorder`). The generated [example certificate](lean/EsmSplit/Instance.lean)
uses `decide +kernel` for `Valid` and applies `split_correct`.
`lean:axioms_test` checks axiom dependencies;
`leanchecker` replays the declarations.

## What is not proved

The abstract theorem assumes footprint soundness for the supplied program.
[`Lower.lean`](lean/EsmSplit/Lower.lean) scans declarations, eager and deferred
names, and assignment sites. [The Effects analysis](effects/README.md) supplies
writes, indirect reads and world effects under its printed premises.
The standalone `split_instance_generator` uses both to write the checked-in example's Lean source without rendering JavaScript modules.

`Meaning` models one flat environment. Using that theorem for JavaScript assumes
that `Hygienic` justifies flattening and that `Plan.visit` agrees with module
evaluation. The [module-order experiment](experiments/README.md#10-does-the-models-module-order-match-node)
compares that order with Node. In the test-only model preview, external imports count as
preexisting bindings. The diagnostic admission check keeps source imports and
exports in the entry. The emitter writes source imports before generated imports. Preserving external initialization order assumes synchronous dependencies
and unchanged resolution of relative specifiers. Diagnostic glue exports may widen
the entry's public surface.

For the publishing splitter, the [CoreJS artifact guarantee](#request-a-corejs-guarantee)
states the parser, binding annotation, lowering, printer, model and compiler premises. The
[footprint proof inventory](docs/footprint_soundness.md#current-proof-inventory)
lists the Effects summaries already covered by separate CoreJS certificates.
For validation of a separately authored tree, see the
[translation validator's renaming premise](translation/README.md#trusted).

## Controls

Specs and plans that must fail. Each failure is a Lean theorem (`¬ OrderOk …`, checked by the kernel) and a real failure in Node.
`lean:example_test` checks the Node behavior.

| Control                                                                                                      | Failing check | Node, when written anyway                                          |
| ------------------------------------------------------------------------------------------------------------ | ------------- | ------------------------------------------------------------------ |
| [`effect_order`](example/controls/effect_order.json): last `console.log` moved into `registry.mjs`           | `OrderOk`     | prints `"\n<circle> 12.56636 true\n"`                              |
| [`cycle_tdz`](example/controls/cycle_tdz.json): `label` in `main.mjs`, which `plugins.mjs` needs and imports | `OrderOk`     | `ReferenceError: Cannot access 'label' before initialization`      |
| [`claimed_twice`](example/controls/claimed_twice.json): two modules pick `label`                             | `Faithful`    | `SyntaxError: Identifier 'label' has already been declared`        |
| `missing_import` (a buggy splitter forgets one import)                                                       | `Hygienic`    | `ReferenceError: registry is not defined`                          |
| `dropped_module` (nobody imports `plugins.mjs`)                                                              | `Permutes`    | prints `"<circle> 12.56636 true\n\n"`; the registrations never run |

[`example/variants/cycle.json`](example/variants/cycle.json) must pass: `isEven` and `isOdd` in separate modules form a real import cycle, which is fine because function declarations exist before any module runs.
`cycle_tdz` is the same cycle with a `const` in it, and fails.

[`Controls.lean`](lean/EsmSplit/Controls.lean) also runs a concrete `Meaning` in the kernel. The accepted splits end in the bundle's state. The two order-violating controls end in a different one.

## Checking the trusted parts

These are tests, not proofs. The Bazel target `fuzz:render_fuzz` runs [`fuzz/fuzz.py`](fuzz/fuzz.py):
510 random placements of the 12 statements, each rendered and run in Node.

- 157 accepted. All 157 print what the bundle prints. 95 of them run statements in a different order than the bundle.
- 353 rejected. 325 really break in Node. 28 are harmless: the checker is conservative.

[`fuzz/mutate.py`](fuzz/mutate.py) weakens the trusted logic and reruns the fuzzer (400 placements per mutant):

| Mutant                                                     | Result                                                                                                |
| ---------------------------------------------------------- | ----------------------------------------------------------------------------------------------------- |
| drop the effect-order conflict                             | caught (18 wrong accepts)                                                                             |
| drop the read-after-declare conflict                       | caught (87)                                                                                           |
| run imports in reverse order                               | caught (4)                                                                                            |
| drop the declare-after-read conflict                       | survives: this bundle reads nothing before its declaration. It matters for `var`.                     |
| no transitive reads (the read set is just the eager names) | caught by a proof: the build fails, because `reads_closed` no longer holds for the changed definition |
| un-mark a module after it runs                             | survives, still sound: `Permutes` rejects the double run                                              |

Running `example/output/` in Node also found a bug in the first printer (no `export` lines). The proof does not cover the printer.

## Further reading

- [Production acceptance and proof work](PLAN.md)
- [Structural checker experiments and Node comparisons](experiments/README.md)
- [Historical scale measurements](experiments/TIMINGS.md)
- [Lessons from the experiment](docs/lessons_learned.md)
- [Historical Excalidraw placement and browser replay](integration/excalidraw/README.md#certified-placement)
