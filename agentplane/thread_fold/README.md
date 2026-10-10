# Thread fold

Folds a Session's raw Event log into the Thread projection its readers see: entity rows, payload
bodies, evidence links, the history summary and per-Session projection leases (`models.py`). The
pure fold is `fold.py`; `recording.py` writes one batch in the caller's transaction, and
`projector.py` pages a Session's history through a caller-supplied reader under a lease.

The History Service runs it over the history database
([Thread fold](../history_service/README.md#thread-fold)). This package imports no `agentplane.app` module. The
tables are declared on this package's own `Base`; a database holding them lists `Base.metadata`
with its migrations and owns their schema. The app database still holds the tables of the app's
retired projection, unread, until a migration drops them.
