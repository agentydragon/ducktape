# Props Frontend

React + Mantine dashboard for Props evaluation results.

```bash
cd props && docker compose up -d postgres registry
bazelisk run //props/frontend:dev       # frontend :5173 + backend :8000, with watch
```

## Visual render-health tests

`//props/frontend:visual` renders component fixtures from `tests/harness/harness.tsx`, selected by
cases in `test_visual.py`, and fails on harness/scenario load failures or uncaught page errors.
No checked-in baselines: rendered PNGs are reviewed on the PR (<../../devinfra/pr_visuals/README.md>).
A new scenario needs a harness fixture plus a `test_visual.py` case; filter with
`--test_filter=DefinitionDetail`.

Issue-overlay colors (TP/FP occurrence, critique match, novel finding) are the Tailwind schemes
in <src/lib/colors.ts>; semantics in <../docs/SPEC.md>.
