"""Re-run a transaction the database aborted for a transient, re-runnable reason.

Postgres resolves a deadlock or a serialization failure by aborting one transaction and asking it
to run again: the statements were not wrong, the interleaving was. A connection dropped under a CNPG
failover is the same shape — the transaction never committed and the same work succeeds on a fresh
connection. `transient_database_error` names that class; `retry_transient_db` re-runs a transaction
function a bounded number of times against it. Anything else — an `IntegrityError`, a programming
error — is the work's own fault and propagates on the first raise.

The retried function must own its whole transaction (open it, commit it) and be safe to run from the
start again: a retry re-executes it on a fresh connection after the aborted one rolled back.
"""

from __future__ import annotations

from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_random_exponential

# The SQLSTATEs Postgres raises for an abort it expects the loser to re-run: serialization failure
# (40001) and deadlock (40P01). The work was correct; only the interleaving lost.
_RERUNNABLE_SQLSTATES = frozenset({"40001", "40P01"})


def transient_database_error(error: BaseException) -> bool:
    """A database error that says nothing about the work: the transaction never committed, and the
    same statements succeed on a healthy connection — a dropped connection (a CNPG failover or
    restart), or an abort Postgres asks the loser to re-run. Never an `IntegrityError` or a
    programming error, which fail identically on retry and so are the work's own."""
    if not isinstance(error, DBAPIError):
        return False
    # `orig` is the dialect-adapted DBAPI error; the asyncpg adapter stamps the server's SQLSTATE
    # onto it as `sqlstate`, and `DBAPIError` offers no typed accessor for it.
    return (
        error.connection_invalidated
        or isinstance(error, (InterfaceError, OperationalError))
        or getattr(error.orig, "sqlstate", None) in _RERUNNABLE_SQLSTATES
    )


# Re-run a transaction function against a transient abort a few times, with short jittered backoff so
# the retries do not re-collide; the last failure propagates once the attempts are spent.
retry_transient_db = retry(
    retry=retry_if_exception(transient_database_error),
    stop=stop_after_attempt(5),
    wait=wait_random_exponential(multiplier=0.05, max=1),
    reraise=True,
)
