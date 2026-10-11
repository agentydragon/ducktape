# Thread fold

Folds a Session's raw Event log into the Thread projection its readers see: entity rows, payload
bodies, evidence links, the history summary and per-Session projection leases (`models.py`). The
pure fold is `fold.py`; `recording.py` writes one batch in the caller's transaction, and
`projector.py` pages a Session's stored history under a lease.

The History Service runs it over the history database ([Thread fold](../README.md#thread-fold)), and
nothing else does. The package is private to `history_service/` except two targets: `views`, the
rows' client contract the browser syncs, and `models`, the tables Electric replicates to it, which
app tests read and seed. The tables are declared on this package's own `Base`; the history
database lists `Base.metadata` with its migrations and owns their schema.
