# augur/study

Reproductions of published results, run through the whole simulator. One subdirectory per
paper.

Every other check on augur is internal — the money math is exact, the fitted model scores well
on its own holdout — and none of it can catch a simulator that is self-consistently wrong. A
study here takes a number somebody else computed from the same history and checks augur lands
near it.

A reproduction is only as useful as its attribution, so each study states, in its docstring,
every way it differs from the paper it reproduces. A residual gap is then either a named
difference or a defect, and never "close enough".

Studies replay the historical record, which comes from the public upstreams
`finance/evidence/sources.py` already specifies — fetched directly rather than through the
augur-evidence mirror, which would need a read credential and supplies nothing a one-shot
reproduction uses. That makes the test targets network-dependent, hence `manual`: they are
excluded from the default CI filter (`.bazelrc` `test:ci --test_tag_filters=…,-manual`) so an
upstream outage cannot redden an unrelated PR. Run one by name.

- <trinity/README.md> — Cooley, Hubbard and Walz (1998), the "4% rule" table.
- <sbbi/README.md> — Ibbotson and Sinquefield (1989), the long-term corporate bond series
  Trinity's bonds are, and augur's only external check on its bond arithmetic.
