"""Budget Beancount exporter runner.

Reads the Plaid mirror, classifies every live transaction with augur's budget
read model (the same classification the in-app budget uses), renders a Beancount
ledger (:mod:`finance.beancount_export.export`), and either writes it to a file
(``render``) or commits it to a git repo (``sync``, used by the CronJob).

The exporter is a pure, deterministic function of (mirror + config): re-running
it produces byte-identical output, so ``sync`` only commits when the ledger
actually changed -- no spurious commits.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
from datetime import date
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import structlog
import typer
import yaml
from beancount import loader
from beancount.parser import printer

from finance.augur.budget.schema import BudgetConfig
from finance.augur.budget.sql_read_model import ClassifiedRow, read_all_classified
from finance.beancount_export.export import ClassifiedTxn, render_ledger
from plaid_utils.schema import async_session_factory

log = structlog.get_logger()

# Where to read the config from when no --config is given (mirrors augur's convention).
_AUGUR_CONFIG_PATH_ENV = "AUGUR_CONFIG_PATH"
_DEFAULT_CONFIG_PATH = Path("/etc/augur/config.yaml")

app = typer.Typer(help="Render / sync the budget Beancount ledger.", no_args_is_help=True)

# Earliest date to scan when the config sets no coverage floor.
_DEFAULT_FLOOR = date(2000, 1, 1)
# Funding leg for any Plaid account not mapped in config.funding_accounts.
_UNLINKED_ACCOUNT = "Equity:Unlinked"

# Module-level typer option singletons (ruff B008: no calls in argument defaults).
_OUT_OPTION = typer.Option(..., help="Path to write the ledger to.")
_CONFIG_OPTION = typer.Option(None, "--config", help="augur config YAML (default: resolve).")
_TITLE_OPTION = typer.Option("Budget", help='Ledger title (option "title").')
_GIT_URL_OPTION = typer.Option(..., help="Ledger repo https URL (creds via GIT_USERNAME/GIT_PASSWORD env).")
_BRANCH_OPTION = typer.Option("main", help="Branch to commit to.")
_LEDGER_NAME_OPTION = typer.Option("main.beancount", help="File path within the repo.")
_AUTHOR_NAME_OPTION = typer.Option("augur budget exporter", help="Commit author name.")
_AUTHOR_EMAIL_OPTION = typer.Option("augur@allegedly.works", help="Commit author email.")


def _load_budget_config(config_path: Path | None) -> BudgetConfig:
    """Parse just the ``budget:`` block from the augur config YAML.

    The exporter only needs the budget config (buckets/rules/overrides/funding), not
    the rest of augur's Config, so it reads the block directly rather than validating
    the whole augur Config -- keeping the exporter independent of the augur server.
    """
    path = config_path or Path(os.environ.get(_AUGUR_CONFIG_PATH_ENV, _DEFAULT_CONFIG_PATH))
    raw = yaml.safe_load(path.read_text())
    budget = raw.get("budget") if isinstance(raw, dict) else None
    if budget is None:
        raise typer.BadParameter(f"no `budget:` block in config {path}")
    return BudgetConfig.model_validate(budget)


def _database_url(config: BudgetConfig) -> str:
    env_var = config.source.database_url_env
    url = os.environ.get(env_var)
    if not url:
        raise typer.BadParameter(f"env var {env_var!r} (budget.source.database_url_env) is not set")
    return url


def _to_txn(row: ClassifiedRow, currency: str) -> ClassifiedTxn:
    return ClassifiedTxn(
        transaction_id=row.transaction_id,
        date=row.date,
        amount=row.amount,
        name=row.name,
        account_id=row.account_id,
        bucket_id=row.bucket_id,
        merchant_name=row.merchant_name,
        pfc_primary=row.pfc_primary,
        pfc_detailed=row.pfc_detailed,
        iso_currency_code=currency,
    )


async def _read_rows(config: BudgetConfig, db_url: str, *, window_end: date) -> tuple[ClassifiedRow, ...]:
    engine, session_factory = async_session_factory(db_url)
    try:
        window_start = config.source.coverage_starts or _DEFAULT_FLOOR
        return await read_all_classified(
            session_factory=session_factory, config=config, window_start=window_start, window_end=window_end
        )
    finally:
        await engine.dispose()


def build_ledger(config: BudgetConfig, db_url: str, *, window_end: date, title: str) -> str:
    """Read + classify + render. Accounts not in funding_accounts fall back to Equity:Unlinked."""
    rows = asyncio.run(_read_rows(config, db_url, window_end=window_end))
    currency = config.source.iso_currency_code
    funding = {f.plaid_account_id: f.account for f in config.funding_accounts}
    for account_id in {row.account_id for row in rows}:
        funding.setdefault(account_id, _UNLINKED_ACCOUNT)
    buckets = {bucket.id: bucket for bucket in config.buckets}
    ledger = render_ledger(
        [_to_txn(row, currency) for row in rows], buckets, funding, title=title, operating_currency=currency
    )
    _validate(ledger)
    log.info("rendered ledger", transactions=len(rows), accounts=len(funding), bytes=len(ledger))
    return ledger


def _validate(ledger: str) -> None:
    """Parse the rendered ledger with beancount; raise if it has any errors.

    Guards the sync path: a malformed ledger (unbalanced, account opened after use,
    bad syntax) fails here instead of being committed to the repo.
    """
    _, errors, _ = loader.load_string(ledger)
    if errors:
        raise RuntimeError(
            "rendered ledger failed beancount validation:\n"
            + "\n".join(printer.format_error(error) for error in errors[:20])
        )


def _authenticated_url(url: str) -> str:
    """Inject GIT_USERNAME/GIT_PASSWORD into an https URL, if both are set."""
    user, password = os.environ.get("GIT_USERNAME"), os.environ.get("GIT_PASSWORD")
    if not (user and password):
        return url
    parts = urlsplit(url)
    netloc = f"{quote(user, safe='')}:{quote(password, safe='')}@{parts.hostname}"
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)
    return result.stdout


@app.command()
def render(out: Path = _OUT_OPTION, config_path: Path | None = _CONFIG_OPTION, title: str = _TITLE_OPTION) -> None:
    """Render the ledger to a local file (no git)."""
    config = _load_budget_config(config_path)
    out.write_text(build_ledger(config, _database_url(config), window_end=date.today(), title=title))
    log.info("wrote ledger", path=str(out))


@app.command()
def sync(
    git_url: str = _GIT_URL_OPTION,
    branch: str = _BRANCH_OPTION,
    ledger_name: str = _LEDGER_NAME_OPTION,
    config_path: Path | None = _CONFIG_OPTION,
    title: str = _TITLE_OPTION,
    author_name: str = _AUTHOR_NAME_OPTION,
    author_email: str = _AUTHOR_EMAIL_OPTION,
) -> None:
    """Render the ledger and commit+push it to the repo, only if it changed."""
    config = _load_budget_config(config_path)
    ledger = build_ledger(config, _database_url(config), window_end=date.today(), title=title)

    url = _authenticated_url(git_url)
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "repo"
        _git(Path(tmp), "clone", "--depth", "1", "--branch", branch, url, str(repo))
        (repo / ledger_name).write_text(ledger)
        if not _git(repo, "status", "--porcelain").strip():
            log.info("ledger unchanged; nothing to commit")
            return
        _git(repo, "add", ledger_name)
        _git(
            repo,
            "-c",
            f"user.name={author_name}",
            "-c",
            f"user.email={author_email}",
            "commit",
            "-m",
            f"budget ledger: {date.today().isoformat()}",
        )
        _git(repo, "push", "origin", branch)
        log.info("pushed ledger", branch=branch, ledger=ledger_name)


if __name__ == "__main__":
    app()
