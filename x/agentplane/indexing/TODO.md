# TODO

- Watch the configured Flux `GitRepository` to trigger artifact reconciliation promptly;
  retain periodic polling to recover from missed events and watch reconnects. Update the
  documented RBAC requirements when adding the watch.
- Add configurable file inclusion/exclusion rules beyond the current UTF-8 eligibility check.
