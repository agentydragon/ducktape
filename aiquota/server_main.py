"""Executable wiring for the aiquota API and its declared frontend bundle."""

from datetime import timedelta
from importlib import resources
from pathlib import Path

import uvicorn

from aiquota.api import (
    BackgroundCollector,
    BrowserOAuth,
    CollectorMetrics,
    HistoryCollector,
    QuotaAPIService,
    SnapshotFeed,
    create_app,
)
from aiquota.clickhouse import ClickHouseSnapshotSink
from aiquota.config import load as load_config
from aiquota.settings import Settings


def _frontend_dir() -> Path:
    frontend_dir = Path(str(resources.files("aiquota") / "frontend" / "dist"))
    if not (frontend_dir / "index.html").is_file():
        raise FileNotFoundError(frontend_dir / "index.html")
    return frontend_dir


def main() -> None:
    settings = Settings()
    service = QuotaAPIService(
        load_config(settings.config_path),
        cache_ttl=settings.cache_ttl,
        claude_proxy=settings.claude_proxy,
        claude_proxy_ca=settings.claude_proxy_ca,
        cli_proxy_api_key=settings.cli_proxy_api_key.get_secret_value() if settings.cli_proxy_api_key else None,
    )
    metrics = CollectorMetrics()
    feed = SnapshotFeed(service)
    sink: ClickHouseSnapshotSink | None = None
    history_collector: HistoryCollector | None = None
    if settings.clickhouse_url:
        assert settings.clickhouse_password is not None
        sink = ClickHouseSnapshotSink(
            url=settings.clickhouse_url,
            username=settings.clickhouse_username,
            password=settings.clickhouse_password.get_secret_value(),
            database=settings.clickhouse_database,
            raw_table=settings.clickhouse_raw_table,
            windows_table=settings.clickhouse_windows_table,
        )
        history_collector = HistoryCollector(
            service, sink, interval=timedelta(seconds=settings.history_interval_seconds), metrics=metrics
        )
    collector = BackgroundCollector(
        feed, sink, interval=timedelta(seconds=settings.poll_interval_seconds), metrics=metrics
    )
    uvicorn.run(
        create_app(
            bearer_token=settings.api_bearer_token,
            fetcher=feed,
            collector=collector,
            history_collector=history_collector,
            metrics=metrics,
            frontend_dir=_frontend_dir(),
            prepare_storage=None if sink is None else sink.apply_schema,
            browser_oauth=BrowserOAuth(
                issuer=settings.oauth_issuer,
                client_id=settings.oauth_client_id,
                client_secret=settings.oauth_client_secret.get_secret_value(),
                session_secret=settings.oauth_session_secret.get_secret_value(),
                public_base_url=settings.public_base_url,
                username=settings.oauth_username,
            ),
        ),
        host="0.0.0.0",
        port=8080,
    )


if __name__ == "__main__":
    main()
