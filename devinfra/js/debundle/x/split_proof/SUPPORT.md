# What the debundler has to support

What a debundler built on this experiment must handle, and what it deliberately does not. This is the requirement, not a claim about what works today: [`README.md`](README.md) says what is checked and proved now.

## Syntax

The intended replacement must support the JavaScript syntax actually used across all configured `gaffer-private` debundle inputs. Broader syntax support is welcome. This is a requirement for the replacement, not a claim that the experiment supports it today.

The available inputs already use the following syntax, so it belongs in that requirement:

- `var`, `let` and `const`; array and object destructuring, default parameters, rest parameters and spread elements. The cached CLI extraction also contains `using` and `await using`.
- Function declarations and expressions, arrow functions, async functions, generators and async generators; calls, `new`, `this`, `super` and `new.target`.
- Classes with `extends`, computed methods and properties, getters and setters, instance and static fields, private fields and methods, and static initialization blocks; object literals with computed keys, methods, getters and setters.
- `if`, `switch`, `for`, `for...in`, `for...of`, `for await...of`, `while`, `do...while`, `try`/`catch`/`finally`, `throw`, labeled statements, `break`, `continue`, `await` and `yield`.
- Template literals and tagged templates, regular-expression and BigInt literals, optional chaining (`?.`), nullish coalescing (`??`), conditional (`?:`) and sequence expressions.
- Binary `+`, `-`, `*`, `/`, `%`, `**`, `<`, `<=`, `>`, `>=`, `==`, `!=`, `===`, `!==`, `&`, `|`, `^`, `<<`, `>>`, `>>>`, `in` and `instanceof`; `&&`, `||`, `??`; unary `!`, `+`, `-`, `~`, `typeof`, `void` and `delete`; `++`, `--`, and assignments `=`, `+=`, `-=`, `*=`, `/=`, `%=`, `&=`, `|=`, `^=`, `<<=`, `>>=`, `>>>=`, `&&=`, `||=` and `??=`.
- Named, default and namespace imports; named and default exports; dynamic `import()` and `import.meta`.

This list is a lower bound, not a complete grammar or an ECMAScript-version claim. A census on 2026-11-20, using `gaffer-private` commit `3ce58b883656` for the committed manifests, parsed 1,107 entries from seven Tana, Claude web and Platform Sessions snapshots, plus 2,256 entries from the Claude CLI extraction, using Acorn 8.19.0 with `ecmaVersion: "latest"`; all parsed without a syntax error. A rerun of the pinned extractor on the cached pinned CLI release, with the configured aliases, produced a byte-identical manifest and files. The generated Tana vendor replacement was also built and parsed; its SHA-256 is `751f15172a28e5c9712535dd568eadedbefbec7b3afb148eb2406d08cb8f0ea9`, and it added no syntax categories to this census. Package-root inputs and other configured fixtures or inputs outside these manifests were not inventoried. All files happened to parse in module grammar; that does not establish their actual host or module semantics. The census did not inspect lexical or regular-expression subfeatures, or runtime builtins. The requirement also covers syntax in configured inputs beyond this measured sample.

## Standard library and host

The same requirement covers the standard library and the host: every standard and host feature the inputs use needs a sound treatment, either a rule of its own or the treatment of unknown code. [`HOST_FEATURES.md`](HOST_FEATURES.md) lists the features by name. An inventory on 2026-11-21 of the 3,363 files that the debundle specs at `gaffer-private` commit `3a48f085d31c` name as input (143.5 MB) found:

- 63 ECMAScript globals, and static paths to most of their members. The names of all but 37 of the 378 members of the standard objects of Node 22 occur somewhere, so the requirement is close to the whole standard library.
- 226 web platform globals, 10 Node.js globals the web does not have, `Bun`, 5 globals of other hosts, 42 Node.js and Bun modules and 5 keys of `import.meta`.
- Assignments to standard objects: `Array.prototype.reduce`, `Function.prototype.toString`, `Math.hypot`, `Object.entries`, `Object.prototype.toString`, `Symbol.dispose` and `Symbol.asyncDispose`. These are patches by name, which the [effect analysis](experiments/effects/README.md) sees (premise P1) and pays for in precision: a patched static member turns the units that read it into effects, and a patched prototype member does that to every unit that is not a function declaration.

The inventory is static and by name: it is a lower bound on the globals and an upper bound on the members of standard objects, and it prints no name that a program or its page defines (270 globals and 805 member paths are counted only). [`experiments/translation/README.md`](experiments/translation/README.md#the-host-inventory) says how to run it.

## Only start-up code needs a precise model

Both lists say what the inputs contain, not what runs when a module is evaluated. A split only changes the order in which top-level code is evaluated, so a precise model is needed only for the code that runs then: the top-level statements and what they call. A function body that is not called at start-up still has to be parsed, carried over unchanged and scanned for the names it mentions and the standard objects it patches, and it counts for what unknown code can reach; it does not have to be evaluated. How much of the syntax and of the standard library runs at start-up is not measured yet. It decides how large a proved model has to be ([`PLAN.md`](PLAN.md) § Direction).

## Not supported

A bundle with one of these is outside the accepted subset. "Refused" means a tool stops with an error; "not detected" means nothing checks it, so it is an assumption about the input.

Decided, not planned:

- **A name declared twice in one list**, `var a = 1, a = 2`. The dump writes one statement per declarator, so this becomes two statements that declare `a`. Refused: `UniqueDefs` rejects the bundle.
- **A top-level `await`**, in the bundle or in a module it imports. Module evaluation is then no longer one sequence: a module imported later can run before the awaiting one finishes.
  - In the bundle: refused. `lowersplit` exits 3 and `translate` rejects the tree.
  - In an imported module: not detected, because the tools do not read the modules a bundle imports.

A scan on 2026-11-21 of the 3,363 input files found neither.

Refused today, open whether to support:

- **`export * from "…"`.** `lowersplit` exits 3: it publishes names that only the other module knows, so a glue export could shadow one.
- **A unit that reads `import.meta`** (except `new URL("/absolute", import.meta.url)`) **or calls `eval` directly.** `translate` rejects the tree: moving or renaming the unit changes what it sees.
- **Several `var` statements that declare one name.** `UniqueDefs` rejects the bundle ([`PLAN.md`](PLAN.md) step 2).
- **Several chunks checked as one program.** Each chunk is checked alone, and an `import` or `export` statement must stay in the entry module ([`PLAN.md`](PLAN.md) step 6).
