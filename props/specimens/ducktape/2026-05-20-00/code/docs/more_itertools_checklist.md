# `more_itertools` patterns to look for

Checklist of manual Python patterns replaceable with `more_itertools` (or stdlib `itertools`).

## Extraction

- [x] `next(iter(x))` → `first(x)` or `one(x)` depending on semantics
- [x] `for i in range(0, len(x), n): batch = x[i:i+n]` → `itertools.batched(x, n)` or `chunked(x, n)` (returns lists)
- [ ] `only(iterable, default=)` — like `one()` but returns default if empty, raises only if >1
- [ ] `strictly_n(iterable, n)` — assert exact length

## Grouping / partitioning

- [ ] Two-list loop: `a, b = [], []; for x in xs: (a if pred(x) else b).append(x)` → `partition(pred, xs)`
- [ ] `defaultdict(list)` grouping without pre-sort → `bucket(iterable, key)`

## Deduplication

- [ ] `seen = set(); result = []; for x in xs: if x not in seen: seen.add(x); result.append(x)` → `unique_everseen(xs)`
- [ ] Same with key function → `unique_everseen(xs, key=)`

## Peeking / windowing

- [ ] `zip(xs, xs[1:])` or manual index pairs → `pairwise(xs)` (stdlib 3.10+) or `windowed(xs, 2)`
- [ ] Sliding window `xs[i:i+n]` in a loop → `windowed(xs, n)`
- [ ] Save-and-check-next patterns → `peekable(iterable)`

## Flattening

- [ ] `[x for sub in lists for x in sub]` → `flatten(lists)` or `itertools.chain.from_iterable(lists)`
- [ ] Recursive flatten → `collapse(iterable)`

## Truthiness / counting

- [ ] `sum(1 for x in xs if pred(x))` → `quantify(xs, pred)`
- [ ] First element matching predicate → `first_true(xs, default, pred)`
- [ ] `all(x == xs[0] for x in xs)` → `all_equal(xs)`

## Consuming

- [ ] `list(itertools.islice(it, n))` → `take(n, it)`
- [ ] Last n elements → `tail(n, iterable)`
- [ ] `for _ in range(n): next(it)` → `consume(it, n)`
