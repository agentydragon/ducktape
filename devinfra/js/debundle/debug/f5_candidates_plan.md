# F5 candidate-list surface: `member_binding_candidates` on the fact resolver

Investigation for the FULL-DELETION track of `AstWildcardMatcher`. Scope: the
**candidate-LIST** surface only (`source_match::member_binding_candidates`), the
last non-categorical member consumer. Sibling agents own `body_debt` diagnostics
and the codemod minimizer; this note stays strictly in the candidate-list lane.

Companion to <../plans/selector_resolver_endpoint.md> (F5, step 1+2).

## VERDICT: GO

The fact resolver can produce a candidate **list** equivalent to
`member_binding_candidates` for every member resolution sub-path. Each of the
four matcher sub-paths already collects an internal `matches: Vec<…>` that the
fact resolver mirrors **one-to-one**, then discards down to the categorical
`[single]/[]/multiple` verdict. The list is recoverable by factoring that
collection out of the categoricity wrapper — no new matching logic, no model
extension. The per-path argument is below.

**Central risk** (and the reason a new differential is mandatory, not optional):
`member_binding_candidates` returns `Vec<ResolvedMemberBinding>` — `binding_name`

- `kind` only, **no `body_idx`** (see `source_match/types.rs:3` and the `.map(|m|
m.binding)` in `binding_resolution.rs:160`). Two distinct owners can therefore
  collapse to equal list elements (same binding name + kind at different body
  indices). The existing corpus differential proves only _categorical_ parity
  (count + winner via `classify_resolver`, `corpus_match_differential.rs:130`), so
  it would not catch a list that has the right length and the right unique winner
  but a different multiset of duplicates, or the right elements in a different
  order. A dedicated candidate-list differential (sequence equality over the real
  corpus, gate = 0 divergences) is required before the consumer can be rerouted.

---

## 1. What the matcher's candidate list is, and how the consumer aggregates

### The list element

`member_binding_candidates(runtime_module, request_id, selector) ->
Result<Vec<ResolvedMemberBinding>>` (`source_match/binding_resolution.rs:153`)
is a thin map over `member_binding_candidate_matches` (`:166`) that drops
`body_idx`:

```rust
member_binding_candidate_matches(runtime_module, request_id, selector)?
    .into_iter()
    .map(|matched| matched.binding)   // MemberBindingMatch.binding : ResolvedMemberBinding
    .collect()
```

`member_binding_candidate_matches` → `member_binding_candidate_matches_within(…,
BodyIndexFilter::All)` (`:166`, `:183`) → `find_member_binding_matches(...,
BodyIndexFilter::All)` (`source_match/target_matching.rs:71`). So the candidate
list is exactly `find_member_binding_matches`'s `Vec<MemberBindingMatch>` with
`body_idx` projected away. `ResolvedMemberBinding` = `{ binding_name: String,
kind: Option<BindingSourceKind> }`, derives `Clone, Eq, PartialEq` (no `Ord`,
no `Hash`) — `types.rs:3`.

### The four matcher sub-paths inside `find_member_binding_matches`

Dispatch (`target_matching.rs:71-157`), in order:

1. **`target_binding` set + single-declarator needle** → `find_matching_target_var_declarators`
   (`:93` → `:305`). Scans `runtime_module.body` in order; for each var-decl
   item, each declarator, runs `prepared.matches_single_var_declarator`; on a
   match pushes `MemberBindingMatch{ body_idx, binding=declared[target_idx] }`.
2. **`target_binding` set + multi-statement needle** → `find_matching_target_bindings`
   (`:103` → `:159`). Sub-dispatches: declarator-holes (`:167`), single-declarator
   target window (`:179` → `find_matching_target_binding_ranges_with_single_declarator`,
   `:213`), else fixed-window range scan (`:189`). All push one
   `MemberBindingMatch` per matched range/declarator, body-index ascending.
3. **no `target_binding` + single-declarator needle** → `find_matching_var_declarators`
   (`:128` → `:619`). Per declarator match; on a multi-binding declarator it
   **bails** (`:652`), else pushes the one binding.
4. **no `target_binding` + general single needle** → `find_matching_body_indices`
   (`:132`, `body_search.rs:14`) then per matched item: 1 binding → push, 0 → skip,
   > 1 → **bail** (`:141`).

Every path either pushes `MemberBindingMatch`es in ascending body-index order or
`bail!`s. **The list is order-significant and ascending by construction** (the
top loops are `runtime_module.body.iter().enumerate()` / `.windows(n).enumerate()`).

### How the consumer aggregates (`anonymous_resolution.rs:107`)

`resolve_member_selector_claims_in_globals` (the CLI edit gate) is the **only**
consumer (confirmed: `grep member_binding_candidates` → just `binding_resolution.rs`
defn, `mod.rs` re-export, and this one call site). It does **cross-source**
aggregation that single-chunk categorical `resolve_member` cannot replicate:

```rust
for selector in claims.selectors {
    let mut matches = Vec::new();
    for parsed in parsed_by_source.values() {            // every chunk source
        matches.extend(member_binding_candidates(&parsed.module, &request_id, selector)?);
    }
    match matches.as_slice() {
        [single] => { if not ImportSpecifier { insert binding_name } }
        []        => bail (no match),
        multiple  => bail (ambiguous, list the names),
    }
}
```

So the cross-source `[single]/[]/multiple` decision happens **after** concatenating
each source's candidate list. This is exactly why the runbook says (line 210)
per-source _categorical_ resolution is not a valid shortcut: a selector that
matches twice **within one source** must contribute 2 to the cross-source count;
a per-source `resolve_member` would collapse that to one error and the
aggregation would mis-count. The candidate **list** (not a per-source verdict) is
the required primitive. Note the consumer only ever reads `binding_name`/`kind`
(never `body_idx`) — it inspects `single.kind` for the `ImportSpecifier`
exclusion and `single.binding_name` for the insert. This is what makes dropping
`body_idx` safe _for the consumer_, but NOT safe for an undisciplined differential
(see central risk).

---

## 2. Per-path evidence: the fact resolver already collects the list

`ChunkResolver::resolve_member` (`datalog_resolver.rs:860`) dispatches to the
same four shapes, each of which builds a `matches: Vec<ResolvedMemberBinding>`
and then collapses it with an identical `[single]/[]/multiple` match. The list
is sitting right there before the collapse in every arm:

| matcher path (`target_matching.rs`)                        | fact path (`datalog_resolver.rs`)                     | `Vec` built before categoricity                      |
| ---------------------------------------------------------- | ----------------------------------------------------- | ---------------------------------------------------- |
| single-declarator, `target_binding` opt. (`:305` / `:619`) | `resolve_var_declarator_member` (`:259`)              | `matches` at `:304`, collapsed `:334`                |
| declarator-hole + `target_binding` (`:377`)                | `resolve_declarator_hole_member` (`:356`)             | `matches` at `:380`, collapsed `:424`                |
| single-decl target window (multi-stmt) (`:213`)            | `resolve_single_declarator_target_window` (`:455`)    | `matches` at `:480`, collapsed `:496`                |
| fixed-window multi-stmt (`:189`)                           | `resolve_member_multi` (`:516`)                       | `matches` at `:563`, collapsed `:574`                |
| general single needle (`:131`)                             | single-statement arm of `resolve_member` (`:895-917`) | `indices` (`Vec<usize>`) at `:897`, collapsed `:898` |

Ordering matches on both sides:

- Fact single-statement scan visits `chunk.candidate_bodies()` — documented
  ascending postings intersection (`datalog_resolver.rs:153-164`,
  `intersect_postings` keeps ascending order). Matcher: `body.iter().enumerate()`.
- Fact multi-statement (`resolve_member_multi`) uses
  `match_fixed_window_sequence_indexed`, which pushes `start` for
  `start in 0..=(len-n)` (`selector_match.rs:1237-1255`) — ascending. Matcher:
  `find_matching_body_ranges`'s `.windows(n).enumerate()` — ascending.
- Fact single-declarator window uses
  `match_single_declarator_target_windows_indexed` (`selector_match.rs:1303`,
  `window_start in 0..=…` ascending; declarators in source order). Matcher:
  `SingleDeclaratorTargetWindow::match_items` recurses declarators in order.
- Fact declarator-hole and single-declarator member iterate
  `candidate_bodies` / `candidate_declarators` (ascending). Matcher iterates
  `body.iter().enumerate()`.

The **categoricity tests already passing** for these paths
(`datalog_resolver.rs` tests `…_categoricity_matches_production` at `:1134`,
`:1185`, `:1241`) prove the fact `matches` vec has the **same length** as the
matcher's in the ambiguous cases — i.e. the count is already equivalent. The new
differential only has to additionally pin element identity + order.

One caveat the differential must surface, not hide: the fact paths bail mid-scan
on some internal inconsistencies (e.g. `resolve_var_declarator_member:322` bails
when `target_binding.is_none()` and a matched declarator binds ≠1 name; the
matcher path `find_matching_var_declarators:652` bails identically). These bails
are **categorical-position** bails that a `member_candidates` accessor must
_preserve_ (return `Err`) to stay faithful — see the body sketch's note.

---

## 3. Refactor plan

### 3a. Factor the categoricity wrapper out of each member sub-path

Today each fact sub-path _both_ collects `matches` _and_ applies the
`[single]/[]/multiple` collapse. Split each into a **collector** returning the
`Vec`, and let `resolve_member` apply the collapse. New private helpers in
`datalog_resolver.rs` (names mirror the existing fns):

```rust
fn member_matches_var_declarator(chunk, needle, request_id, export_name, selector)
    -> Result<Vec<ResolvedMemberBinding>>;          // body of :259 up to :333
fn member_matches_declarator_hole(chunk, needle, request_id, export_name, selector)
    -> Result<Vec<ResolvedMemberBinding>>;          // body of :356 up to :423
fn member_matches_single_declarator_window(chunk, needles, request_id, export_name, selector,
        target_item_idx, target_binding_idx)
    -> Result<Vec<ResolvedMemberBinding>>;          // body of :455 up to :495
fn member_matches_multi(chunk, needles, request_id, export_name, selector)
    -> Result<Vec<ResolvedMemberBinding>>;          // body of :516 up to :573
fn member_matches_single_statement(chunk, needle, request_id, export_name, selector)
    -> Result<Vec<ResolvedMemberBinding>>;          // body of :895 up to :916
```

Each collector keeps every existing **pre-collapse** `bail!` (parse errors,
unsupported-needle probes, "binds ≠1 name", "index out of range", "matched by a
hole") — those are faithful to the matcher, which also `bail!`s in the same
positions inside `find_member_binding_matches`. Only the **final**
`match matches.as_slice() { [single]/[]/multiple }` collapse is removed from the
collector and lifted to the caller.

Add a shared categoricity helper to keep `resolve_member` terse (mirrors the
existing `one_group_match` at `:841`):

```rust
fn one_member_match(
    matches: Vec<ResolvedMemberBinding>,
    request_id: &str,
    export_name: &str,
) -> Result<ResolvedMemberBinding> {
    match matches.as_slice() {
        [single] => Ok(single.clone()),
        []        => bail!("logical_module {request_id}: members[].selector.source_match for \
                            export `{export_name}` did not match any declaration in the chunk"),
        multiple  => bail!("logical_module {request_id}: members[].selector.source_match for \
                            export `{export_name}` is ambiguous — matched {}", multiple.len()),
    }
}
```

(The five existing per-arm error messages differ slightly in wording — "did not
match any declarator" vs "any top-level statement range" etc. Those messages are
only read by humans/`reject_parity`, never asserted for value equality. Collapsing
to one message is fine and removes duplication; if message fidelity is wanted,
keep them in the collectors as today and have the collector return a typed
"empty/ambiguous" — but that re-imports the very collapse we are factoring out.
Recommend the single shared message.)

### 3b. New public method on the seam

Add to `ChunkResolver` (NOT to the `SelectorResolver` trait — the trait is the
_categorical_ seam and `AstWildcardResolver` has no list method to mirror;
keeping `member_candidates` inherent to `ChunkResolver` matches how the matcher
exposes `member_binding_candidates` as a free fn, not a trait method):

```rust
impl ChunkResolver<'_> {
    /// The non-categorical match list for a member-form selector — the fact-side
    /// twin of `source_match::member_binding_candidates`. Shares the per-path
    /// match collection with `resolve_member`; the only difference is this returns
    /// the whole list instead of collapsing to the unique winner.
    pub fn member_candidates(
        &self,
        request_id: &str,
        selector: &AnonymousStatementSelector,
    ) -> Result<Vec<ResolvedMemberBinding>> {
        let needles = parse_needles(request_id, selector)?;
        let export_name = "candidate";   // diagnostics-only; matches the matcher
                                          // free fn taking no export name
        let [needle] = needles.as_slice() else {
            return member_matches_multi(self, &needles, request_id, export_name, selector);
        };
        if selector_var_decl_has_declarator_holes(needle) {
            return member_matches_declarator_hole(self, needle, request_id, export_name, selector);
        }
        if selector_single_var_declarator(needle).is_some() {
            return member_matches_var_declarator(self, needle, request_id, export_name, selector);
        }
        member_matches_single_statement(self, needle, request_id, export_name, selector)
    }
}
```

and `resolve_member` (`:860`) becomes the same dispatch wrapping each collector
in `one_member_match(...)`. Multi-statement arm dispatches identically — note
`member_matches_multi` itself still needs the internal single-declarator-window
branch (`:531`) calling `member_matches_single_declarator_window`; that nested
collapse-free call is fine because the window collector now returns a `Vec` too.

Re-export `member_candidates`? It is a method, so `pub fn` on the inherent impl
suffices; `ChunkResolver` is already `pub use`d (`mod.rs:91`).

**Signature requested by the runbook** is
`member_candidates(request_id, selector) -> Result<Vec<ResolvedMemberBinding>>` —
matched exactly (no `export_name`, since the matcher free fn has none either).

### 3c. Categoricity stays single-sourced

After 3a/3b, `resolve_member` and `member_candidates` share the five collectors
verbatim. The `[single]/[]/multiple` logic exists once (`one_member_match`).
`member_binding_candidate_matches_within`'s `BodyIndexFilter` parameter has **no
fact analogue** and is not needed: its only non-`All` use is the matcher's own
internal candidate-index prefilter; the consumer at `anonymous_resolution.rs:107`
always passes `All` (via `member_binding_candidates`). The fact resolver's token
index already is its sound prefilter. So `member_candidates` takes no filter.

---

## 4. Candidate-list differential design

### Where it lives

Extend the **existing** `corpus_match_differential.rs` binary (it already loads
every member selector, builds one `ChunkResolver` per chunk, and runs the matcher
oracle side-by-side). Add a parallel pass next to the member resolver pass
(`corpus_match_differential.rs:450-474` in `run`, and the per-chunk
`resolve_case` member arm at `:741`). This reuses the proven corpus plumbing
(`PER_CHUNK_JS_ROOT`, the `tana/re` snapshot) and the existing gate-printing.

### What it compares

For each member selector, both lists over the **same** chunk module:

- fact: `chunk.member_candidates(request_id, selector)`
- matcher: `source_match::member_binding_candidates(module, request_id, selector)`

Both return `Result<Vec<ResolvedMemberBinding>>`. Compare with the **same
agreement rule** as `classify_resolver` but on the whole `Vec`:

```rust
fn classify_candidate_lists(
    tally: &mut ResolverTally,
    fact: &Result<Vec<ResolvedMemberBinding>>,
    matcher: &Result<Vec<ResolvedMemberBinding>>,
) {
    match (fact, matcher) {
        (Ok(f), Ok(m)) if f == m => tally.resolved_parity += 1,  // Vec<_> : PartialEq = elementwise+order
        (Ok(_), Ok(_))           => tally.value_disagreements += 1,
        (Err(_), Err(_))         => tally.reject_parity += 1,
        (Err(_), Ok(_))          => tally.fail_closed += 1,
        (Ok(_), Err(_))          => tally.over_resolved += 1,
    }
}
```

### Equivalence definition (the load-bearing decision)

**Ordered sequence equality of `Vec<ResolvedMemberBinding>`** — i.e. derived
`Vec::eq`: same length, same elements (`binding_name` + `kind`), in the same
order. Justification:

- **Sequence, not set**: a set comparison would mask a duplicate-count
  divergence — exactly the intra-source ambiguity the runbook (line 210) says
  must be caught. The cross-source consumer's `[single]/[]/multiple` decision is
  driven by `matches.len()` _after_ `extend`, so the length (hence duplicate
  multiplicity) is semantically load-bearing. A set would also be impossible to
  build directly: `ResolvedMemberBinding` derives neither `Ord` nor `Hash`.
- **Order matters and is free**: both sides emit ascending-body-index order
  (§2). Requiring order equality is therefore not over-strict — it is satisfied
  by construction whenever the underlying matches agree — and it is strictly more
  diagnostic than a multiset compare (catches an ordering regression in either
  scan). If a future change made the two orders legitimately differ, that is a
  real divergence worth a human look, not something to paper over.
- **Identity of each element = `(binding_name, kind)`**, NOT `body_idx`:
  `member_binding_candidates` discards `body_idx`, and the consumer never reads
  it, so the contract being replaced is name+kind only. Comparing `body_idx`
  would over-constrain beyond the consumer's actual dependency. (If extra safety
  is wanted, a _separate_ `member_binding_candidate_matches` vs a `body_idx`-
  carrying fact accessor could be diffed — but that exceeds the surface being
  replaced and is not recommended for the gate.)

### How it's gated

Reuse the existing gate arithmetic. The candidate-list pass feeds its own
`ResolverTally`; add it to the `genuine` sum
(`corpus_match_differential.rs:564` / `:929`):

```
genuine += candidates.value_disagreements + candidates.over_resolved;
```

Gate condition unchanged in spirit: **value_disagreements == 0 AND
over_resolved == 0** for the candidate-list tally (fail-closed is the shrink-to-
zero worklist, identical to the member pass). The runbook's "gate = 0
divergences" maps to `value_disagreements + over_resolved == 0` on the candidate
tally; for FULL deletion the bar is stronger — also `fail_closed == 0` (the fact
list must handle every member shape the matcher list does, since it becomes the
sole implementation). Print the issues with the existing `Issue`/`classify_and_
record` machinery (`:649`) so any divergence is identifiable, not just counted.

### Why the existing differential does not already cover this

`corpus_match_differential` compares `resolve_member` (categorical:
`Result<ResolvedMemberBinding>`) vs `AstWildcardResolver::resolve_member`
(`:454-456`, `:751`). That proves count==1 cases and the unique winner. It says
nothing about the **list shape** when count≠1, because in those cases both sides
are `Err` and land in `reject_parity` — agreement, regardless of how many
candidates each found or whether the candidate identities matched. The
candidate-list pass is precisely the missing coverage for the count≠1 and
duplicate-multiplicity cases.

---

## 5. Consumer to reroute once proven

`anonymous_resolution.rs:107`, inside
`resolve_member_selector_claims_in_globals`. After the candidate-list
differential is green (= 0) over the real corpus:

- Build one `ChunkResolver` per parsed source (mirror the co-move path's
  `resolvers_by_source` at `:289-297`, which already does exactly this for
  `resolve_anonymous_groups`).
- Replace the inner loop body (`:106-112`)

  ```rust
  for parsed in parsed_by_source.values() {
      matches.extend(source_match::member_binding_candidates(&parsed.module, &request_id, selector)?);
  }
  ```

  with

  ```rust
  for (source_path, _parsed) in &parsed_by_source {
      matches.extend(resolvers_by_source[source_path].member_candidates(&request_id, selector)?);
  }
  ```

The cross-source `[single]/[]/multiple` arbitration (`:113-137`) is unchanged —
it operates on the concatenated `matches` vec exactly as before. The
`ImportSpecifier` exclusion (`:115`) keys off `single.kind`, which
`member_candidates` preserves (the collectors carry the binding's `kind` through
unchanged — e.g. `declared_bindings(...)[idx]` is the same `ResolvedMemberBinding`
the matcher uses; both call into the shared `declared_bindings` module).

After this reroute, the matcher (`find_member_binding_matches` +
`member_binding_candidate*` + `member_binding_candidates`) is no longer reached
for _resolution_; it remains reached only by `body_debt` diagnostics, the codemod
minimizer, and the differential's own oracle — the surfaces the sibling agents /
the differential-retirement step own. Deleting it is then their concern, not the
candidate-list lane's.

---

## Open checks before coding (not blockers)

- The five collectors' `export_name` param is diagnostics-only. `member_candidates`
  has no export name (matches the matcher free fn). Passing a fixed `"candidate"`
  placeholder is fine; alternatively drop `export_name` from the collectors and
  thread it only in `resolve_member`'s error path. Minor.
- `resolve_member`'s single-statement arm currently inlines the `indices ->
declared_bindings -> nth(target_binding_idx)` logic (`:895-917`) rather than
  building a `Vec<ResolvedMemberBinding>`. `member_matches_single_statement` must
  reproduce the per-index push (matcher path `target_matching.rs:131-156`:
  1 binding → push, 0 → skip, >1 → bail). The fact arm today asserts exactly one
  index then `bail!`s on ≠1 binding; as a collector it must instead loop the
  matched indices and push/skip/bail per item to match the matcher list. This is
  the one arm whose list form is not a literal lift of existing code — verify it
  against `find_matching_body_indices` + the `[single]/[]/_=>bail` block. The
  candidate-list differential will catch any divergence here.
- Build/test gate (when RBE available): `source_match_test`,
  `corpus_match_differential` builds; then the corpus list-differential run = 0.
  RBE is unavailable in this investigation container, so this plan is from static
  reading only — no build was run.
