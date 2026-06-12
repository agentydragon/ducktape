use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Duration;

use anyhow::Result;
use axum::body::Body;
use axum::extract::{OriginalUri, State};
use axum::http::{HeaderMap, HeaderName, HeaderValue, Response, StatusCode, header};
use axum::{Router, routing::get};
use log::info;
use wayback_archive::{
    AdaptiveLimiter, ArchiveResponse, ArchiveService, ArchiveStore, DEFAULT_MAX_BODY_BYTES,
    DEFAULT_MAX_METADATA_BYTES, DEFAULT_MAX_QUEUE_WAIT, LimiterConfig, MemoryArchiveStore,
    PostgresArchiveStore, ReqwestIaClient, S3BlobStore,
};

#[tokio::main]
async fn main() -> Result<()> {
    env_logger::init();
    let port = std::env::var("PORT")
        .ok()
        .and_then(|value| value.parse::<u16>().ok())
        .unwrap_or(8080);
    let web_upstream = std::env::var("WAYBACK_ARCHIVE_WEB_UPSTREAM")
        .unwrap_or_else(|_| "https://web.archive.org".to_string());
    let availability_upstream = std::env::var("WAYBACK_ARCHIVE_AVAILABILITY_UPSTREAM")
        .unwrap_or_else(|_| "https://archive.org".to_string());
    let max_body_bytes = std::env::var("WAYBACK_ARCHIVE_MAX_BODY_BYTES")
        .ok()
        .and_then(|value| value.parse::<usize>().ok())
        .unwrap_or(DEFAULT_MAX_BODY_BYTES);
    let max_metadata_bytes = std::env::var("WAYBACK_ARCHIVE_MAX_METADATA_BYTES")
        .ok()
        .and_then(|value| value.parse::<usize>().ok())
        .unwrap_or(DEFAULT_MAX_METADATA_BYTES);
    let queue_wait = std::env::var("WAYBACK_ARCHIVE_MAX_QUEUE_WAIT_SECONDS")
        .ok()
        .and_then(|value| value.parse::<u64>().ok())
        .map(Duration::from_secs)
        .unwrap_or(DEFAULT_MAX_QUEUE_WAIT);

    let store: Arc<dyn ArchiveStore> =
        if let Ok(database_url) = std::env::var("WAYBACK_ARCHIVE_DATABASE_URL") {
            info!("using Postgres metadata store and S3 blob store");
            let blobs = Arc::new(S3BlobStore::from_env()?);
            Arc::new(PostgresArchiveStore::new(database_url, blobs).await?)
        } else {
            info!("using in-memory archive store");
            Arc::new(MemoryArchiveStore::new())
        };
    let replay_limiter = Arc::new(AdaptiveLimiter::new(LimiterConfig {
        queue_wait,
        ..LimiterConfig::replay()
    }));
    let availability_limiter = Arc::new(AdaptiveLimiter::new(LimiterConfig {
        queue_wait,
        ..LimiterConfig::availability()
    }));
    let cdx_limiter = Arc::new(AdaptiveLimiter::new(LimiterConfig {
        queue_wait,
        ..LimiterConfig::cdx()
    }));
    let service = Arc::new(
        ArchiveService::new(
            store,
            Arc::new(ReqwestIaClient::new(web_upstream, availability_upstream)?),
        )
        .with_endpoint_limiters(
            availability_limiter,
            cdx_limiter,
            replay_limiter,
            queue_wait,
        )
        .with_max_body_bytes(max_body_bytes)
        .with_max_metadata_bytes(max_metadata_bytes),
    );

    let app = Router::new()
        .route("/healthz", get(|| async { "ok\n" }))
        .route("/readyz", get(|| async { "ok\n" }))
        .route("/metrics", get(metrics))
        .fallback(handle)
        .with_state(service.clone());
    let address = SocketAddr::from(([0, 0, 0, 0], port));
    info!("wayback archive listening on {address}");
    let listener = tokio::net::TcpListener::bind(address).await?;

    if let Some(auth_token) = std::env::var("WAYBACK_ARCHIVE_AUTH_TOKEN")
        .ok()
        .filter(|value| !value.is_empty())
    {
        let auth_port = std::env::var("WAYBACK_ARCHIVE_AUTH_PORT")
            .ok()
            .and_then(|value| value.parse::<u16>().ok())
            .unwrap_or(8090);
        let auth_address = SocketAddr::from(([0, 0, 0, 0], auth_port));
        let auth_listener = tokio::net::TcpListener::bind(auth_address).await?;
        let auth_state = Arc::new(AuthedState {
            service,
            authorization_header: HeaderValue::from_str(&format!("Bearer {auth_token}"))?,
        });
        let auth_app = Router::new()
            .route("/healthz", get(handle_authed_health))
            .route("/readyz", get(handle_authed_health))
            .fallback(handle_authed)
            .with_state(auth_state);
        info!("wayback archive bearer-authed listener on {auth_address}");
        tokio::try_join!(async { axum::serve(listener, app).await }, async {
            axum::serve(auth_listener, auth_app).await
        },)?;
    } else {
        axum::serve(listener, app).await?;
    }
    Ok(())
}

#[derive(Clone)]
struct AuthedState {
    service: Arc<ArchiveService>,
    authorization_header: HeaderValue,
}

async fn handle(
    State(service): State<Arc<ArchiveService>>,
    OriginalUri(uri): OriginalUri,
) -> Response<Body> {
    into_axum_response(service.handle_request(uri.path(), uri.query()).await)
}

async fn handle_authed(
    State(state): State<Arc<AuthedState>>,
    OriginalUri(uri): OriginalUri,
    headers: HeaderMap,
) -> Response<Body> {
    if !is_authorized(&state, &headers) {
        return unauthorized_response();
    }

    into_axum_response(state.service.handle_request(uri.path(), uri.query()).await)
}

async fn handle_authed_health(
    State(state): State<Arc<AuthedState>>,
    headers: HeaderMap,
) -> Response<Body> {
    if !is_authorized(&state, &headers) {
        return unauthorized_response();
    }
    Response::builder()
        .status(StatusCode::OK)
        .body(Body::from("ok\n"))
        .expect("response construction should not fail")
}

fn is_authorized(state: &AuthedState, headers: &HeaderMap) -> bool {
    headers.get(header::AUTHORIZATION) == Some(&state.authorization_header)
}

fn unauthorized_response() -> Response<Body> {
    Response::builder()
        .status(StatusCode::UNAUTHORIZED)
        .header(header::WWW_AUTHENTICATE, "Bearer")
        .body(Body::from("unauthorized\n"))
        .expect("response construction should not fail")
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

fn into_axum_response(response: ArchiveResponse) -> Response<Body> {
    let mut builder = Response::builder()
        .status(StatusCode::from_u16(response.status).unwrap_or(StatusCode::BAD_GATEWAY));
    for (name, value) in response.headers {
        if let (Ok(name), Ok(value)) = (
            HeaderName::from_bytes(name.as_bytes()),
            HeaderValue::from_str(&value),
        ) {
            builder = builder.header(name, value);
        }
    }
    builder
        .body(Body::from(response.body))
        .expect("response construction should not fail")
}
