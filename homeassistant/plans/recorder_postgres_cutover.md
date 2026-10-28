# Recorder cutover to Postgres

The remaining cutover acceptance check is to confirm that the Home Assistant
History panel renders records from before the cutover. Keep the SQLite file and
its VolSync snapshots until that check passes. If it fails, follow the rollback
noted in [#11813](https://github.com/agentydragon/ducktape/pull/11813). Delete
this plan once the check is complete.
