# Selector resolution: specification

What `debundle` guarantees when it places spec entities in a bundle chunk. How to
write selectors: <docs/selectors.md>. How it is implemented:
<docs/selector_resolution.md>.

## Entities

An **entity** is one thing a spec places in a chunk: a module member, a
`source_matches[].bindings[]` entry, or an `anonymous_statements[]` entry. Each
entity's selector names the places it may occupy: a top-level declaration (by the
minified binding it declares) or a group of top-level statements.

An entity is scoped to its module's chunk, and only that chunk's places are its
candidates. In a tree spec, each module tree names the chunk its modules are
scoped to; several trees may name the same chunk, and their modules then share
it as if authored in one tree.

## Matching

Only the shape matcher decides where a `source_match` template matches. A
template matches a place when their syntax trees are equal up to:

- **holes** — `ANYTHING`, `EXPR`, `STMT`, `STMT_LIST`, `DECLARATORS` and the other
  hole keywords of <docs/selectors.md> match any subtree of their kind;
- **alpha-renaming** — binding and value identifiers match any identifier,
  consistently within their lexical scope.

An anonymous statement written with a bare `match:` rather than `source_match:`
skips alpha-renaming: its identifiers must equal the chunk's. Literals,
operators, member property names, object keys and tree structure are
significant. Relational selectors (`cross_ref`, `reads_member`, …) match through
facts derived from the chunk, not through templates.

A selector matching more than 100 places is `too_broad` and takes no further
part.

## Assignment

Every chunk's entities form one program. Each entity takes exactly one of its
matched places, jointly with every other entity:

- no two entities claim the same place, whichever modules or trees they come
  from;
- a relational selector holds between the places its entities take.

The answer is the **unique** assignment satisfying these. An assignment that
holds only for some solutions of the joint problem is not an answer.

## Outcomes

Every entity gets exactly one outcome, in one record format shared by `run`,
`spec validate` and `spec match-selector`:

| Outcome                                 | Meaning                                                                                           | Severity |
| --------------------------------------- | ------------------------------------------------------------------------------------------------- | -------- |
| `resolved`, `resolved_by: own_selector` | its selector alone matches one place                                                              | ok       |
| `resolved`, `resolved_by: elimination`  | unique only because the named `claimers` took its other places                                    | warning  |
| `no_match`                              | no place satisfies its selector and constraints                                                   | error    |
| `ambiguous`                             | several assignments exist; at most 5 of its places are listed, with `truncated` when more exist   | error    |
| `conflict`                              | no assignment exists; `with` names the entities of an unsatisfiable set (not necessarily minimal) | error    |
| `too_broad`                             | its selector matches more than 100 places                                                         | error    |
| `duplicate_claim`                       | resolved to a binding another entity already claims                                               | error    |
| `invalid`                               | its selector does not parse, uses an unsupported construct, or its matches do not map to places   | error    |
| `undecided`                             | the solver stopped before deciding it                                                             | error    |

A contradiction affects only the entities in it: the others still resolve.

Every command that resolves a spec — `run`, `spec validate` in both modes, the
edit gate and `describe` — gives each entity the same outcome. Only the pipeline
(`run`, `spec validate --spec`) reports `duplicate_claim`. `spec match-selector`
resolves its selector as a spec of one entity, so a selector resolved by
elimination in a spec is `ambiguous` there.

## Modes

- **Keep-going** (the default): every outcome is reported, entities with an
  error outcome stay unclaimed, and a chunk with any error outcome fails once
  all of its outcomes are reported. Every chunk is reported; the run fails
  with the first failing chunk in chunk-id order.
- **Fail-fast** (`--fail-fast`): the first error outcome stops the run. Outcomes
  come in a fixed order: every chunk's duplicate claims, then every chunk's
  resolved outcomes, chunks in chunk-id order.

Warnings never stop a run.
