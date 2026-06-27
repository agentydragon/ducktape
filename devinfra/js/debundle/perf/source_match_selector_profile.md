# source_match declaration-hole selector profile

This note tracks stack-sample evidence for generic `source_match`
selectors shaped like large direct literal sweeps. It intentionally uses
only synthetic names and source.

## Budget

Selector-heavy agent workflows should be interactive: under 10 seconds on
warmed inputs is the target, and sustained runs over 60 seconds on the largest
known downstream specs are priority performance bugs unless the command is
explicitly marked as an offline/profile mode. New matcher/index work should
show either lower wall time or a material drop in timed selector resolutions on
a broad workload, not only a microbenchmark win.

## Workload

Target:

```bash
bazelisk run //devinfra/js/debundle/perf:source_match_decl_holes_profile -- \
  --mode binding-group --declarations 600 --declarators 10 --selectors 600 --repetitions 1
```

The generated chunk contains many top-level `const` declarations, each
with many string-literal declarators. Each selector is a one-declaration
template with leading/trailing `DECLARATORS_*` holes and one pinned
`STR_LITERAL_MATCHING_RE("^generic-token-...$")` declarator.

Modes:

- `binding-group`: calls `resolve_member_binding_group`, matching the
  preferred `binding_groups[].source_match` shape.
- `target-binding`: calls member-form `resolve_member_binding` with
  `target_binding`, a comparable single-binding resolution path.
- `ambiguity`: calls member-form `resolve_member_binding` without
  `target_binding`, so matching the wider declaration produces the
  multi-binding ambiguity diagnostic shape from direct-selector sweeps.

## Profiles

### Parent private-corpus signal

A downstream CSS-direct validation run spent about 9 minutes CPU-bound
before emitting hundreds of member-form `source_match` ambiguity
diagnostics. The captured profile is not committed here because it
belongs to a private corpus, but the stack-sample shape is relevant:

```bash
perf record -F 99 -g -p <debundle-pid> \
  -o /tmp/debundle-css-direct.perf.data -- sleep 30
```

That run captured 2968 samples and was dominated by regex setup:
`regex_automata::meta::strategy::new`,
`regex_automata::nfa::thompson::compiler::*`,
range-trie / alphabet byte-class construction, `regex_syntax::ast::parse`,
and allocation/free around regex structures. The selectors all used
`STR_LITERAL_MATCHING_RE(...)` in member-form selectors over
multi-declarator top-level declarations. That pointed at repeated regex
parse/compile during candidate matching, not just expensive regex search.

### Generic before profile

Command:

```bash
perf record -F 99 -g \
  -o /tmp/ducktape-source-match-generic-before.perf.data -- \
  bazel-bin/devinfra/js/debundle/perf/source_match_decl_holes_profile \
    --declarations 60 --declarators 8 --selectors 60 --repetitions 1
```

Timing:

```text
source_match_decl_holes_profile declarations=60 declarators=8 selectors=60 repetitions=1 elapsed_ms=9580 checksum=710
```

An unprofiled `/usr/bin/time` run of the same command took
`elapsed=14.78 user=11.10 sys=0.06`. The profile captured 944 samples.
This pre-PR workload resolved one pinned declarator per selector; the
same code path fed both member target-binding selectors and
binding-group declarator-hole resolution.
Top stacks included:

- `regex_automata::hybrid::dfa::Lazy::add_state`
- `regex_automata::nfa::thompson::backtrack::Config::new`
- `regex_automata::util::alphabet::ByteClasses::set`
- `regex_automata::nfa::thompson::compiler::Compiler::c_concat`
- `regex_syntax::ast::parse::*`

The generic profile reproduced the private-corpus signal: selector
resolution was repeatedly compiling regexes while scanning declarations.

### Optimization

`source_match` now prepares regex predicates once per parsed selector
needle and passes them through the wildcard matcher. For
`DECLARATORS_*` variable-declaration selectors, it also builds a cheap
prefilter from pinned direct string-literal predicates. Candidate
declarations whose string-literal initializers cannot satisfy those
predicates are rejected before recursive declarator-hole alignment.

This covers both target-binding declarator-hole selectors and the
member-form no-`target_binding` path that otherwise only reaches the
"matched a multi-binding declaration" ambiguity diagnostic after a full
AST match.

### Generic after optimization

Same small workload, after the optimization:

```text
mode=Ambiguity     declarations=60 declarators=8 selectors=60 elapsed_ms=115 elapsed=0.12 user=0.11
mode=TargetBinding declarations=60 declarators=8 selectors=60 elapsed_ms=144 elapsed=0.16 user=0.14
mode=BindingGroup  declarations=60 declarators=8 selectors=60 elapsed_ms=151 elapsed=0.16 user=0.13
```

Scaled binding-group run:

```text
source_match_decl_holes_profile mode=BindingGroup declarations=600 declarators=10 selectors=600 repetitions=1 elapsed_ms=6267 checksum=7690
elapsed=6.34 user=6.24 sys=0.00
```

### Generic binding-group after profile

```bash
perf record -F 99 -g \
  -o /tmp/ducktape-source-match-binding-group-after.perf.data -- \
  bazel-bin/devinfra/js/debundle/perf/source_match_decl_holes_profile \
    --mode binding-group --declarations 600 --declarators 10 --selectors 600 --repetitions 1
```

Timing:

```text
source_match_decl_holes_profile mode=BindingGroup declarations=600 declarators=10 selectors=600 repetitions=1 elapsed_ms=6481 checksum=7690
```

The binding-group after profile captured 639 samples. Top stacks moved from regex
compiler/setup to regex search and prefilter work:

- `regex_automata::hybrid::search::find_fwd`
- `regex_automata::hybrid::dfa::DFA::next_state_untagged_unchecked`
- `regex_automata::meta::regex::Regex::search_half`
- `source_match::StringLiteralPredicate::matches`
- `source_match::VarDeclWithDeclaratorHolesPrefilter::var_decl_can_match`

That is the intended shape: regexes are still used to test candidate
string literals, but regex parse/compile setup is no longer on the hot
path for every candidate AST comparison.

## Remaining Work

The next algorithmic step is a chunk-level source-match index keyed by
top-level declaration kind and direct literal values/predicates. The
current change keeps the public resolver API unchanged and still scans
candidate declarations per distinct selector, but most candidates are
rejected before recursive AST matching and regex compilation is hoisted
out of that loop.

Keep-going miss diagnostics can also do repeated nearest-candidate scans
for selectors that truly miss. This PR targets the observed slow
ambiguity path; miss-diagnostic caching/indexing should be handled as a
follow-up if a profile shows `source_match_no_match_hint` dominating.

### Selector Synthesis Dogfood: Filter Latency

A downstream large-spec run of `debundle spec synthesize-selectors --rewrite
name-binding-to-source-match` on a single 6.9 MiB / 204k-line chunk showed the
next performance blocker is command-level filtering and plan application, not
only the inner declaration-hole matcher. No private source text is reproduced
here.

Observed elapsed times from an optimized merged binary:

| Scope                    | Elapsed | Candidate changes | Notes                                       |
| ------------------------ | ------: | ----------------: | ------------------------------------------- |
| one explicit `--item`    |   3.43s |                 0 | still scanned 1745 files / 6692 members     |
| `--module-prefix` subset |    >30s |                 - | timed out CPU-bound before producing output |
| top-100 explicit items   |  16.37s |                75 | still scanned 1745 files / 6692 members     |
| top-200 explicit items   |  31.38s |               157 | still scanned 1745 files / 6692 members     |

The source-aware selector synthesis path is productive, but broad dogfood is
blocked until item/file/module filters prune YAML traversal and candidate
generation earlier. The acceptance target for this workload is:

- one explicit `--item` should be close to source parse + one module file scan,
  not a full spec scan;
- top-100 explicit items should stay within the interactive budget on warmed
  inputs, ideally under 10s;
- broad runs should stream progress or declare themselves offline/profile mode
  if they cannot meet the budget.
