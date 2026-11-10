# EnvironmentService TODOs

- [ ] Replace the Python-emulating `pythonJSON` in
      [`internal/management/access.go`](internal/management/access.go) with deterministic intent
      encoding and compare caller/request identity semantically rather than requiring serialized
      annotation bytes to match. Preserve same-caller/request adoption after an uncertain Kubernetes
      create and conflict for a different caller or request. Cover Unicode/HTML strings, identical
      retries, different callers/requests, and uncertain create results; Python-byte interoperability
      is not a requirement.
