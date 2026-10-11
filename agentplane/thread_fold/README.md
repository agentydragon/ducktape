# Thread fold

Folds a Session's raw Event log into the Thread projection its readers see: entity rows, payload
bodies, evidence links, the history summary and per-Session projection leases (`models.py`). The
pure fold is `fold.py`; `recording.py` writes one batch in the caller's transaction, and
`projector.py` pages a Session's history under a lease, from the History Service's API or, inside
the History Service, its store.

This package imports no `agentplane.app` module, so the app and the History Service can both run
it, each over its own database. The tables are declared on this package's own `Base`; a database
holding them lists `Base.metadata` with its migrations and owns their schema.
