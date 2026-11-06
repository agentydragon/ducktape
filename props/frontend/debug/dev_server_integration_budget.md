# Dev launcher integration test budget, 2026-11-06

`//props/frontend:test_dev_server_integration` starts PostgreSQL, migrates a fresh
database, builds the frontend and starts the real backend. Its 60-second action
budget expired during active setup/startup on cold RBE workers:

- [CI failure](https://app.buildbuddy.io/invocation/765c4c26-17cc-5b05-9d83-00cdfc4bc834):
  setup logs began at action second 29; PostgreSQL was ready at second 54 and
  migrations finished at second 54.4. The launcher had reached esbuild watch.
- [Focused reproduction](https://app.buildbuddy.io/invocation/d8821ff6-4a27-4e8d-9f6b-b3130d99a237):
  pytest completed the assertion and all fixture teardown in 42.42 seconds, but the
  complete action still timed out at 60.2 seconds.
- [Bounded diagnostic run](https://app.buildbuddy.io/invocation/cbb7ab47-ec9d-45eb-a9a8-6628eb02b4d0):
  with a temporary 120-second action limit, the test exited successfully in 92.4 seconds.
  PostgreSQL image load took 27.8 seconds and container startup another 6.9 seconds.

Recent successful target-history results were 52.308 and 58.049 seconds; repeated
cached results are not independent timing samples. `size = "medium"` covers this
cold integration lifecycle. The test's 45-second readiness deadline, launcher's
30-second backend wait, assertions and cleanup remain unchanged.
