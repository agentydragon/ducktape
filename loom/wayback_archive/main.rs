use std::net::SocketAddr;
use std::sync::Arc;

use anyhow::Result;
use axum::body::Body;
use axum::extract::{OriginalUri, State};
use axum::http::{HeaderMap, HeaderValue, Response, StatusCode, header};
use axum::{Router, routing::get};
use log::info;
use wayback_archive::{
    ArchiveInputService, ArchiveResponse, archive_config_from_env, archive_store_from_config,
};

#[tokio::main]
async fn main() -> Result<()> {
    env_logger::init();
    let config = archive_config_from_env()?;
    let settings = config.settings();
    let service = Arc::new(
        ArchiveInputService::new(archive_store_from_config(&config).await?)
            .with_queue_wait(settings.queue_wait),
    );

    let app = Router::new()
        .route("/healthz", get(|| async { "ok\n" }))
        .route("/readyz", get(|| async { "ok\n" }))
        .route("/metrics", get(metrics))
        .fallback(handle)
        .with_state(service.clone());
    let address = SocketAddr::from(([0, 0, 0, 0], settings.port));
    info!("listening on {address}");
    let listener = tokio::net::TcpListener::bind(address).await?;

    if let Some(auth_token) = settings.auth_token {
        let auth_address = SocketAddr::from(([0, 0, 0, 0], settings.auth_port));
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
        info!("bearer-authed listener on {auth_address}");
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
    service: Arc<ArchiveInputService>,
    authorization_header: HeaderValue,
}

async fn handle(
    State(service): State<Arc<ArchiveInputService>>,
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

async fn metrics(State(service): State<Arc<ArchiveInputService>>) -> Response<Body> {
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
    let mut builder = Response::builder().status(response.status);
    for (name, value) in response.headers.iter() {
        builder = builder.header(name, value);
    }
    builder
        .body(Body::from(response.body))
        .expect("response construction should not fail")
}
