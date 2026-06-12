use std::net::SocketAddr;
use std::sync::Arc;

use anyhow::Result;
use axum::body::Body;
use axum::extract::State;
use axum::http::{Response, StatusCode, header};
use axum::{Router, routing::get};
use log::info;
use wayback_archive::{
    AdaptiveLimiter, ArchiveFiller, ArchiveService, LimiterConfig, ReqwestIaClient,
    archive_config_from_env, archive_store_from_config,
};

#[tokio::main]
async fn main() -> Result<()> {
    env_logger::init();
    let config = archive_config_from_env()?;
    let settings = config.settings();
    let store = archive_store_from_config(&config).await?;
    let replay_limiter = Arc::new(AdaptiveLimiter::new(LimiterConfig {
        queue_wait: settings.queue_wait,
        ..LimiterConfig::replay()
    }));
    let availability_limiter = Arc::new(AdaptiveLimiter::new(LimiterConfig {
        queue_wait: settings.queue_wait,
        ..LimiterConfig::availability()
    }));
    let cdx_limiter = Arc::new(AdaptiveLimiter::new(LimiterConfig {
        queue_wait: settings.queue_wait,
        ..LimiterConfig::cdx()
    }));
    let service = Arc::new(
        ArchiveService::new(
            store.clone(),
            Arc::new(ReqwestIaClient::with_timeouts(
                settings.web_upstream,
                settings.availability_upstream,
                settings.timeouts,
            )?),
        )
        .with_endpoint_limiters(
            availability_limiter,
            cdx_limiter,
            replay_limiter,
            settings.queue_wait,
        )
        .with_max_body_bytes(settings.max_body_bytes)
        .with_max_metadata_bytes(settings.max_metadata_bytes),
    );
    let app = Router::new()
        .route("/healthz", get(|| async { "ok\n" }))
        .route("/readyz", get(|| async { "ok\n" }))
        .route("/metrics", get(metrics))
        .with_state(service.clone());
    let address = SocketAddr::from(([0, 0, 0, 0], settings.port));
    info!("metrics listener on {address}");
    let listener = tokio::net::TcpListener::bind(address).await?;

    tokio::try_join!(
        async {
            axum::serve(listener, app)
                .await
                .map_err(anyhow::Error::from)
        },
        async { ArchiveFiller::new(store, service).run_forever().await },
    )?;
    Ok(())
}

async fn metrics(State(service): State<Arc<ArchiveService>>) -> Response<Body> {
    match service.metrics().await {
        Ok(metrics) => Response::builder()
            .status(StatusCode::OK)
            .header(
                header::CONTENT_TYPE,
                "text/plain; version=0.0.4; charset=utf-8",
            )
            .body(Body::from(metrics))
            .expect("response construction should not fail"),
        Err(error) => Response::builder()
            .status(StatusCode::INTERNAL_SERVER_ERROR)
            .header(header::CONTENT_TYPE, "text/plain; charset=utf-8")
            .body(Body::from(format!("metrics encoding failed: {error}\n")))
            .expect("response construction should not fail"),
    }
}
