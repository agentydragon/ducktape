# Call-argument selector replacement experiment

## Decision

Retain `passed_to_call`. Use-site `source_matches[]` covers the common
registration shapes but does not provide a drop-in replacement for its
nonlocal declaration-kind filter. No production selector code changed. Do not
extend the matcher solely to remove this unused selector; reconsider if a real
spec needs a different contract. `member_of_module` and import-source identity
were outside this experiment.

The current private spec tree has no `passed_to_call` YAML references (checked
with `rg` over `*.yaml` / `*.yml`). That establishes no current caller, not
semantic equivalence with another selector.

## What the CLI proves

The positive probes check module source and execute the emitted entry under
Node; rejection probes run `debundle run --dry-run` and check typed selector
outcomes. The entry keeps unclaimed registration calls. Tests:

- <../e2e/use_site_pinning_test.rs>
- <../e2e/passed_to_call_lowering_test.rs>

| Existing selector behavior                       | Source-template probe                                                                                 | Result                                                                                |
| ------------------------------------------------ | ----------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------- |
| Select a separately declared registration target | Claim the free argument in `appRegistry.register(ARGS_before, Service, ARGS_after);`                  | Selects and renames the class without claiming the call                               |
| Restrict the registry object                     | Resolve `appRegistry` by a declaration template; register different empty classes with two registries | Selects only the anchored registry's argument                                         |
| Ignore raw identifier spelling                   | Repeat that fixture with renamed registry and class bindings                                          | Same authored templates and emitted behavior                                          |
| Restrict argument index                          | Match `unclaimedReceiver.define(EXPR_name, Target, ARGS_after);`                                      | Selects the second argument, leaving the other class unclaimed                        |
| Reject ambiguity                                 | Two different bindings passed to `.register`                                                          | `ambiguous`                                                                           |
| Reject an absent callee                          | Only `.other` is called                                                                               | `no_match`                                                                            |
| Reject a non-identifier argument                 | `.register(new A())`                                                                                  | `no_match` for the claimed argument                                                   |
| Reject a failed registry anchor                  | Declare an `appRegistry` entity whose selector has no match                                           | Whole spec fails with `no_match` for that anchor                                      |
| Filter the target's declaration kind             | A class and a function are registered; a second identical class is not                                | Relational selector succeeds; call-only and class-only templates are each `ambiguous` |

An undeclared free receiver name is **not** an anchor reference in the template
language: it alpha-renames. The argument-position probe deliberately uses
`unclaimedReceiver` against raw `host`. Unlike `passed_to_call.object`, a typo
that removes the last spec entity with that name need not fail. A declared
anchor whose own selector fails still makes the whole spec fail. These are
different contracts, not interchangeable spellings of an unknown-anchor error.

## Minimal declaration-kind witness

```js
const registry = {
  register(x) {
    (this.items ||= []).push(x);
  },
};
class A {}
class B {}
function helper() {}
console.log("before registration");
registry.register(A);
registry.register(helper);
```

`passed_to_call: { callee_member: register, kind: class_declaration }` selects
`A`. A call template sees both `A` and `helper`; a class template sees both `A`
and `B`. The class and call are not adjacent. Multi-statement templates match
fixed contiguous windows; spanning these statements would encode unrelated
neighbors. Adding a separate anonymous-statement claim to constrain the class
would also claim the call, changing the target-only ownership contract. The
existing source-template binding claim has no independent declaration-kind
filter to conjoin with its use-site candidate.

The regression test exercises all three selector choices against this same
source. The successful relational selection emits the class into `services`,
leaves registration out of that module, and prints
`before registration\nServiceClass 2 B\n` under Node.

## Reproduce

```sh
bbr test //devinfra/js/debundle/e2e:use_site_pinning_test \
  //devinfra/js/debundle/e2e:passed_to_call_lowering_test
```
